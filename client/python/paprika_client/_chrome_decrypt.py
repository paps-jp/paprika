"""Operator-side Chrome cookie / Login Data decryption.

Used by ``paprika-client upload-profile --decrypt-cookies`` to
unwrap the OS-encrypted parts of a Chrome profile BEFORE uploading
the tarball to a Paprika Hub. Without this step, cookies stored
by Windows Chrome are encrypted with a DPAPI-derived AES key the
Linux worker can never recover, so the uploaded profile looks
"logged out" in noVNC.

What we do, per OS:

  * Windows: read ``Local State`` JSON, base64-decode the
    ``os_crypt.encrypted_key`` field, strip the ``DPAPI`` magic
    prefix, call ``CryptUnprotectData`` (via ctypes, no pywin32
    dependency), get the 32-byte AES master key.
  * macOS: query Keychain for "Chrome Safe Storage" / "Chromium
    Safe Storage", derive the master key via PBKDF2(password,
    salt='saltysalt', 1003 iterations, 16 bytes).
  * Linux: same as macOS but with 1 PBKDF2 iteration (matches
    Chrome's source).

Then for every row in ``Default/Cookies`` where ``encrypted_value``
starts with ``v10`` or ``v20``:

  * Strip prefix
  * AES-GCM decrypt (key = master key, nonce = next 12 bytes)
  * For Chrome v117+ (``v20``) the plaintext is prepended with a
    32-byte SHA256(host_key) binding -- strip if present
  * Write the resulting plaintext into the ``value`` column,
    NULL out ``encrypted_value``

The Linux worker's Chrome then reads ``Cookies`` and, finding
``encrypted_value`` empty, uses ``value`` directly. Logged-in
state preserved across OS without needing to forge any DPAPI
state on the worker side.

NOT decrypted by default (yet):
  * Login Data (saved passwords) -- same scheme but a different
    schema; flip on with ``decrypt_login_data=True``.
  * Web Data / Network -- not security-sensitive in practice.

After rewriting, the ``Local State.os_crypt`` block is also
stripped so the worker's Chrome generates a fresh key on first
write rather than trying to decrypt the now-stale Windows
``encrypted_key``.
"""
from __future__ import annotations

import base64
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Optional


# --- OS-specific master-key recovery -------------------------------------


def _master_key_windows(local_state_path: Path) -> Optional[bytes]:
    """DPAPI-decrypt the AES master key out of Local State.

    Returns 32 bytes on success, ``None`` if the OS isn't Windows /
    DPAPI fails (e.g. profile was created by a different user)."""
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    try:
        ls = json.loads(Path(local_state_path).read_text(encoding="utf-8"))
        enc_b64 = ls["os_crypt"]["encrypted_key"]
    except Exception as e:
        print(
            f"  warn: could not read os_crypt.encrypted_key from "
            f"{local_state_path}: {type(e).__name__}: {e}",
            file=sys.stderr,
        )
        return None

    enc = base64.b64decode(enc_b64)
    if not enc.startswith(b"DPAPI"):
        print(
            f"  warn: encrypted_key missing 'DPAPI' magic "
            f"(first bytes: {enc[:8]!r}); not a Windows Chrome "
            f"profile?",
            file=sys.stderr,
        )
        return None
    enc = enc[len(b"DPAPI"):]

    # ctypes signature for CryptUnprotectData -- avoids the
    # pywin32 dependency (~80 MB install).
    class DATA_BLOB(ctypes.Structure):
        _fields_ = [
            ("cbData", wintypes.DWORD),
            ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
        ]

    in_buf = ctypes.create_string_buffer(enc, len(enc))
    in_blob = DATA_BLOB(
        len(enc),
        ctypes.cast(in_buf, ctypes.POINTER(ctypes.c_ubyte)),
    )
    out_blob = DATA_BLOB()
    if not ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(in_blob), None, None, None, None, 0,
        ctypes.byref(out_blob),
    ):
        err = ctypes.windll.kernel32.GetLastError()
        print(f"  warn: CryptUnprotectData failed (GetLastError={err})",
              file=sys.stderr)
        return None
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out_blob.pbData)


def _master_key_macos() -> Optional[bytes]:
    """Pull Chrome's keychain password and derive the AES key.

    Calls ``security find-generic-password -wa Chrome`` -- prompts
    the operator for keychain access via macOS UI on first run.
    Returns 16 bytes (PBKDF2-derived AES-128 key).
    """
    if sys.platform != "darwin":
        return None
    import subprocess
    for service in ("Chrome", "Chromium"):
        try:
            r = subprocess.run(
                ["security", "find-generic-password", "-wa", service],
                check=True, capture_output=True, text=True,
            )
            password = r.stdout.strip()
            if password:
                return _pbkdf2_chrome_key(password.encode(), iterations=1003)
        except subprocess.CalledProcessError:
            continue
    print(
        "  warn: could not read Chrome keychain password "
        "(security find-generic-password -wa Chrome failed)",
        file=sys.stderr,
    )
    return None


def _master_key_linux() -> Optional[bytes]:
    """Try libsecret first, then the BASIC fallback.

    Linux Chrome uses kFallbackKey ('peanuts') when no keyring is
    available, so a fresh Chrome install in a container would just
    use 'peanuts'. A real desktop install usually goes through
    libsecret. We try the real path first via the ``secretstorage``
    library (optional); on failure we fall back to 'peanuts' which
    is what the worker's Chrome will use anyway -- meaning a
    Linux-to-Linux profile transfer would Just Work after decrypt.
    """
    if sys.platform == "win32" or sys.platform == "darwin":
        return None
    try:
        import secretstorage
        bus = secretstorage.dbus_init()
        coll = secretstorage.get_default_collection(bus)
        for item in coll.get_all_items():
            if item.get_label() in ("Chrome Safe Storage", "Chromium Safe Storage"):
                password = item.get_secret()
                return _pbkdf2_chrome_key(password, iterations=1)
    except Exception:
        pass
    # BASIC fallback.
    return _pbkdf2_chrome_key(b"peanuts", iterations=1)


def _pbkdf2_chrome_key(password: bytes, *, iterations: int) -> bytes:
    """Chrome's password -> AES-128 key derivation (used on Mac/Linux)."""
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    from cryptography.hazmat.primitives import hashes
    return PBKDF2HMAC(
        algorithm=hashes.SHA1(),
        length=16,
        salt=b"saltysalt",
        iterations=iterations,
    ).derive(password)


def detect_master_key(local_state_path: Path) -> Optional[bytes]:
    """Auto-detect the right master-key recovery path for this OS.
    Returns the master key bytes or None on failure."""
    if sys.platform == "win32":
        return _master_key_windows(local_state_path)
    if sys.platform == "darwin":
        return _master_key_macos()
    return _master_key_linux()


# --- Cookie value decryption ---------------------------------------------


def decrypt_chrome_value(enc: bytes, key: bytes, host_key: str = "") -> Optional[str]:
    """Decrypt one Cookies.encrypted_value blob.

    Handles three Chrome wire formats:
      * ``v10`` (Win/Mac/Linux modern): AES-GCM, nonce=enc[3:15],
        ciphertext+tag=enc[15:].
      * ``v20`` (Chrome 117+ on Windows): same AES-GCM layout but
        the first 32 plaintext bytes are SHA256(host_key) used as
        a binding token; strip them if they match.
      * Anything else (legacy ``v11``, plain): return None so the
        caller can leave the row alone.
    """
    if len(enc) < 16 or not enc.startswith((b"v10", b"v20")):
        return None
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        nonce = enc[3:15]
        body = enc[15:]
        plain = AESGCM(key).decrypt(nonce, body, None)
    except Exception:
        return None
    # v20 (Chrome 117+) prepends SHA256(host_key) as a binding.
    if enc.startswith(b"v20") and host_key:
        import hashlib
        bind = hashlib.sha256(host_key.encode("utf-8")).digest()
        if plain.startswith(bind):
            plain = plain[len(bind):]
    try:
        return plain.decode("utf-8")
    except UnicodeDecodeError:
        # Cookie values are usually ASCII / URL-encoded but defend
        # against weird encodings rather than dropping the cookie.
        return plain.decode("latin-1", errors="replace")


# --- Cookies SQLite rewrite ----------------------------------------------


def rewrite_cookies_plaintext(
    cookies_path: Path, master_key: bytes,
) -> dict:
    """Decrypt every cookie row and stash the plaintext in the
    ``value`` column so a Linux Chrome that can't access the OS
    keyring still reads them.

    Returns a stats dict::

        {
            "v10_decrypted": int,   # legacy Chrome <= 126 cookies, decrypted OK
            "v20_skipped":   int,   # Chrome 127+ App-Bound Encryption,
                                    # not decryptable without SYSTEM / IElevator
            "other_skipped": int,   # unknown / corrupt rows
        }
    """
    stats = {"v10_decrypted": 0, "v20_skipped": 0, "other_skipped": 0}
    if not cookies_path.exists():
        return stats
    conn = sqlite3.connect(str(cookies_path))
    try:
        conn.text_factory = str
        cur = conn.cursor()
        cur.execute("PRAGMA journal_mode = DELETE;")
        cur.execute(
            "SELECT host_key, name, path, encrypted_value "
            "FROM cookies WHERE LENGTH(encrypted_value) > 3"
        )
        rows = cur.fetchall()
        for host_key, name, path, enc in rows:
            if isinstance(enc, str):
                enc = enc.encode("latin-1")
            # v20 (Chrome 127+) cookies need the app-bound key which
            # is SYSTEM-DPAPI-protected. We can identify but not
            # decrypt them with user-level credentials -- Paprika
            # Bridge is the supported fallback for these.
            if enc.startswith(b"v20"):
                stats["v20_skipped"] += 1
                continue
            plain = decrypt_chrome_value(enc, master_key, host_key=host_key)
            if plain is None:
                stats["other_skipped"] += 1
                continue
            cur.execute(
                "UPDATE cookies SET value = ?, encrypted_value = x'' "
                "WHERE host_key = ? AND name = ? AND path = ?",
                (plain, host_key, name, path),
            )
            stats["v10_decrypted"] += 1
        conn.commit()
    finally:
        conn.close()
    return stats


def strip_local_state_os_crypt(local_state_path: Path) -> None:
    """Remove the os_crypt block so worker Chrome (which can't
    decrypt the OS-bound master key anyway) generates a fresh one
    on first write.

    Also keeps the upload smaller -- the encrypted_key is the only
    field in os_crypt that matters for Chrome's startup.
    """
    if not local_state_path.exists():
        return
    try:
        ls = json.loads(local_state_path.read_text(encoding="utf-8"))
    except Exception:
        return
    if "os_crypt" in ls:
        ls.pop("os_crypt", None)
        local_state_path.write_text(
            json.dumps(ls, ensure_ascii=False), encoding="utf-8",
        )


# --- High-level entry point ----------------------------------------------


def decrypt_profile_inplace(profile_root: Path) -> dict:
    """Walk a cloned Chrome profile snapshot at ``profile_root``
    and rewrite the OS-encrypted parts to OS-agnostic plaintext.

    Returns a stats dict for logging::

        {
            "key_found": bool,
            "v10_decrypted": int,   # cookies decrypted to plaintext
            "v20_skipped":   int,   # Chrome 127+ App-Bound Encryption
            "other_skipped": int,   # unrecognised format
        }

    Note on v20: Chrome 127+ wraps cookies in an App-Bound layer
    whose key requires SYSTEM-level DPAPI access (or Chrome's
    IElevator COM interface) to unwrap. User-level processes
    cannot decrypt these; for v20 traffic the operator should use
    the Paprika Bridge extension which gets cookies via the
    chrome.cookies API (Chrome itself decrypts before exposing
    them to the extension, so v10 vs v20 is invisible at that
    layer).
    """
    stats = {
        "key_found": False,
        "v10_decrypted": 0,
        "v20_skipped": 0,
        "other_skipped": 0,
    }
    local_state = profile_root / "Local State"
    cookies = profile_root / "Default" / "Cookies"
    network_cookies = profile_root / "Default" / "Network" / "Cookies"

    key = detect_master_key(local_state)
    if key is None:
        return stats
    stats["key_found"] = True

    for cookies_path in (cookies, network_cookies):
        if cookies_path.exists():
            s = rewrite_cookies_plaintext(cookies_path, key)
            for k, v in s.items():
                stats[k] += v

    strip_local_state_os_crypt(local_state)
    return stats
