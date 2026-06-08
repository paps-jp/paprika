"""paprika-client CLI.

Entry point installed by ``pip install paprika-client`` as the
``paprika-client`` script. Also reachable via
``python -m paprika_client``.

Subcommands:

  upload-profile   Snapshot the operator's local Chrome profile,
                   tar+gzip it, and POST it to the hub so jobs can
                   reference it via ``options.use_profile``.
  list-profiles    Print every uploaded profile (name + size +
                   timestamps) so operators can see what's there
                   without curl-ing /profiles.
  delete-profile   Remove an uploaded profile from the hub.

Common options:

  --hub URL        Base URL of the paprika hub. Falls back to the
                   PAPRIKA_HUB environment variable, then to
                   http://localhost:8000.

Examples::

    # Snapshot the default Chrome profile and upload as "mydefault".
    paprika-client upload-profile --name mydefault --hub http://paprika.lan:8000

    # List uploaded profiles.
    paprika-client list-profiles --hub http://paprika.lan:8000

    # Delete one.
    paprika-client delete-profile --name old --hub http://paprika.lan:8000
"""
from __future__ import annotations

import argparse
import os
import platform
import shutil
import socket
import sys
import tarfile
import tempfile
from pathlib import Path
from typing import Optional


_DEFAULT_HUB = "http://localhost:8000"


def _resolve_hub(explicit: Optional[str]) -> str:
    return (
        (explicit or "").strip()
        or os.environ.get("PAPRIKA_HUB", "").strip()
        or _DEFAULT_HUB
    ).rstrip("/")


def _auth_headers(args: argparse.Namespace) -> dict:
    """Bearer auth header from ``--token`` or $PAPRIKA_API_KEY/$PAPRIKA_TOKEN,
    or ``{}`` when none is set. Needed once a hub runs auth_mode=enforce; a
    no-op (anonymous) against off/optional hubs."""
    tok = (
        getattr(args, "token", None)
        or os.environ.get("PAPRIKA_API_KEY")
        or os.environ.get("PAPRIKA_TOKEN")
    )
    return {"Authorization": f"Bearer {tok}"} if tok else {}


def _machine_label() -> str:
    """Short string the hub can show as 'uploaded from' info."""
    try:
        return f"{platform.system()} {platform.release()} ({socket.gethostname()})"
    except Exception:
        return platform.system() or "unknown"


# ---------------------------------------------------------------- upload

def _cmd_upload_profile(args: argparse.Namespace) -> int:
    import httpx  # imported lazily so the CLI starts fast for --help
    from paprika_client._chrome_local import (
        clone_local_chrome_profile,
        ProfileCloneError,
    )

    hub = _resolve_hub(args.hub)
    name = args.name.strip()
    if not name:
        print("error: --name is required", file=sys.stderr)
        return 2

    # 1) Snapshot the local Chrome profile to a scratch dir.
    print(f"==> cloning local Chrome profile {args.chrome_profile!r} ...",
          file=sys.stderr)
    try:
        snapshot = clone_local_chrome_profile(
            args.chrome_profile,
            extras=tuple(args.include or ()),
        )
    except ProfileCloneError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    # 1b) Decrypt the OS-bound parts (cookies + master key) so the
    # Linux worker that has no DPAPI / Keychain / libsecret can
    # still read them. See paprika_client/_chrome_decrypt.py for
    # the full rationale. Skipped when --no-decrypt-cookies is
    # set (operator may want the raw profile for some reason --
    # e.g. uploading from Linux desktop to Linux worker where the
    # same kFallbackKey is used end-to-end).
    if not args.no_decrypt_cookies:
        from paprika_client._chrome_decrypt import decrypt_profile_inplace
        print("==> decrypting cookies for cross-OS compatibility ...",
              file=sys.stderr)
        stats = decrypt_profile_inplace(snapshot)
        if not stats["key_found"]:
            print(
                "    warn: could not derive Chrome's master key on "
                "this OS. Cookies will be left encrypted (and "
                "unreadable on the worker). Non-cookie parts of "
                "the profile (Preferences / Bookmarks / Local "
                "Storage / IndexedDB) still transfer.",
                file=sys.stderr,
            )
        else:
            v10 = stats["v10_decrypted"]
            v20 = stats["v20_skipped"]
            other = stats["other_skipped"]
            print(
                f"    v10 cookies decrypted: {v10}\n"
                f"    v20 cookies skipped:   {v20} "
                f"(Chrome 127+ App-Bound Encryption)\n"
                f"    other rows skipped:    {other}",
                file=sys.stderr,
            )
            if v20 > 0:
                print(
                    "\n    NOTE: Chrome 127 added App-Bound "
                    "Encryption (v20) that user-level processes "
                    "cannot decrypt. For sites whose cookies need "
                    "to survive the upload (login state), install "
                    "the Paprika Bridge extension and push "
                    "cookies via chrome.cookies API:\n"
                    "      http://<your-hub>/profiles/extension/install\n"
                    "    The uploaded profile still carries Local "
                    "Storage / IndexedDB / Preferences / Bookmarks.",
                    file=sys.stderr,
                )

    # 2) Tarball it. The hub-side worker extracts the archive directly
    # into the lane's user-data-dir, so the archive's top-level must
    # be the User Data tree itself (no wrapping dir).
    tar_path = Path(tempfile.mkstemp(
        prefix=f"paprika_upload_{name}_", suffix=".tar.gz",
    )[1])
    print(f"==> packaging tarball {tar_path} ...", file=sys.stderr)
    try:
        with tarfile.open(tar_path, "w:gz", compresslevel=6) as tar:
            for child in snapshot.iterdir():
                tar.add(child, arcname=child.name)
        size = tar_path.stat().st_size
        print(f"    {size:,} bytes", file=sys.stderr)
    except Exception as e:
        print(f"error: tar build failed: {type(e).__name__}: {e}",
              file=sys.stderr)
        return 1
    finally:
        shutil.rmtree(snapshot, ignore_errors=True)

    # 3) POST the body as application/gzip. Streaming so a 100 MB
    # upload doesn't load into RAM.
    headers = {
        "Content-Type": "application/gzip",
        "X-Paprika-Source-Machine": _machine_label(),
        "X-Paprika-Chrome-Profile": args.chrome_profile,
    }
    if args.note:
        headers["X-Paprika-Note"] = args.note
    url = f"{hub}/profiles/{name}"
    print(f"==> POST {url} ({size:,} bytes) ...", file=sys.stderr)
    try:
        with open(tar_path, "rb") as f:
            with httpx.Client(timeout=300.0) as cli:
                r = cli.post(url, content=f, headers=headers)
    except Exception as e:
        print(f"error: upload failed: {type(e).__name__}: {e}",
              file=sys.stderr)
        return 1
    finally:
        try:
            tar_path.unlink()
        except OSError:
            pass

    if r.status_code >= 400:
        print(f"error: hub returned {r.status_code}: {r.text}",
              file=sys.stderr)
        return 1
    body = r.json()
    print(
        f"==> uploaded as {name!r}: "
        f"{body.get('size_human') or body.get('size_bytes')} "
        f"(uploaded_at {body.get('uploaded_at')})",
        file=sys.stderr,
    )
    print(
        "    invoke with: "
        "POST /jobs body={\"options\": {\"use_profile\": "
        + repr(name)
        + "}}",
        file=sys.stderr,
    )
    return 0


# ---------------------------------------------------------------- list

def _cmd_list_profiles(args: argparse.Namespace) -> int:
    import httpx

    hub = _resolve_hub(args.hub)
    try:
        with httpx.Client(timeout=30.0) as cli:
            r = cli.get(f"{hub}/profiles")
    except Exception as e:
        print(f"error: hub unreachable: {type(e).__name__}: {e}",
              file=sys.stderr)
        return 1
    if r.status_code >= 400:
        print(f"error: hub returned {r.status_code}: {r.text}",
              file=sys.stderr)
        return 1
    body = r.json()
    profiles = body.get("profiles") or []
    default_name = body.get("default")
    if not profiles:
        print("(no profiles uploaded)")
        return 0
    print(f"{'NAME':<24} {'SIZE':>10}  {'UPLOADED':<25}  SOURCE")
    for p in profiles:
        # Star-prefix the default so it stands out at a glance.
        mark = "*" if p.get("is_default") else " "
        print(
            f"{mark}{p.get('name',''):<23} "
            f"{p.get('size_human',''):>10}  "
            f"{p.get('uploaded_at',''):<25}  "
            f"{(p.get('source_machine') or '-')}"
        )
    if default_name:
        print(f"(default = {default_name!r} -- auto-applied when "
              f"options.use_profile is omitted)")
    return 0


# ---------------------------------------------------------------- default

def _cmd_set_default_profile(args: argparse.Namespace) -> int:
    import httpx

    hub = _resolve_hub(args.hub)
    name = (args.name or "").strip()
    try:
        with httpx.Client(timeout=30.0) as cli:
            if name:
                r = cli.post(f"{hub}/profiles/{name}/default")
            else:
                # No --name: clear the default.
                r = cli.delete(f"{hub}/profiles/default")
    except Exception as e:
        print(f"error: hub unreachable: {type(e).__name__}: {e}",
              file=sys.stderr)
        return 1
    if r.status_code >= 400:
        print(f"error: hub returned {r.status_code}: {r.text}",
              file=sys.stderr)
        return 1
    body = r.json()
    if body.get("name"):
        print(f"default profile set to {body['name']!r}", file=sys.stderr)
    else:
        prev = body.get("previous")
        if prev:
            print(f"default profile cleared (was {prev!r})", file=sys.stderr)
        else:
            print("no default profile is set", file=sys.stderr)
    return 0


# ---------------------------------------------------------------- delete

def _cmd_delete_profile(args: argparse.Namespace) -> int:
    import httpx

    hub = _resolve_hub(args.hub)
    name = args.name.strip()
    if not name:
        print("error: --name is required", file=sys.stderr)
        return 2
    try:
        with httpx.Client(timeout=30.0) as cli:
            r = cli.delete(f"{hub}/profiles/{name}")
    except Exception as e:
        print(f"error: hub unreachable: {type(e).__name__}: {e}",
              file=sys.stderr)
        return 1
    if r.status_code >= 400:
        print(f"error: hub returned {r.status_code}: {r.text}",
              file=sys.stderr)
        return 1
    body = r.json()
    if body.get("deleted"):
        print(f"deleted profile {name!r}", file=sys.stderr)
    else:
        print(f"(profile {name!r} did not exist)", file=sys.stderr)
    return 0


# ---------------------------------------------------------------- auth

def _cmd_auth_whoami(args: argparse.Namespace) -> int:
    import httpx  # lazy so --help stays fast
    hub = _resolve_hub(args.hub)
    r = httpx.get(f"{hub}/auth/me", headers=_auth_headers(args), timeout=30)
    print(r.text)
    return 0 if r.status_code == 200 else 1


def _cmd_auth_key_create(args: argparse.Namespace) -> int:
    import httpx
    hub = _resolve_hub(args.hub)
    body: dict = {"name": args.name or ""}
    if args.user_id:
        body["user_id"] = args.user_id
    r = httpx.post(
        f"{hub}/auth/keys", json=body, headers=_auth_headers(args), timeout=30,
    )
    if r.status_code != 200:
        print(f"error {r.status_code}: {r.text}", file=sys.stderr)
        return 1
    data = r.json()
    key = data.get("key", {})
    # The full plaintext key — printed to STDOUT so it can be piped/captured.
    # It is shown exactly once and is NOT retrievable later.
    print(data.get("secret", ""))
    print(
        f"created key id={key.get('id')} prefix={key.get('prefix')} "
        "— store the line above (printed to stdout); it cannot be shown again.",
        file=sys.stderr,
    )
    return 0


def _cmd_auth_key_list(args: argparse.Namespace) -> int:
    import httpx
    hub = _resolve_hub(args.hub)
    r = httpx.get(f"{hub}/auth/keys", headers=_auth_headers(args), timeout=30)
    print(r.text)
    return 0 if r.status_code == 200 else 1


def _cmd_auth_key_revoke(args: argparse.Namespace) -> int:
    import httpx
    hub = _resolve_hub(args.hub)
    r = httpx.delete(
        f"{hub}/auth/keys/{args.id}", headers=_auth_headers(args), timeout=30,
    )
    if r.status_code != 200:
        print(f"error {r.status_code}: {r.text}", file=sys.stderr)
        return 1
    print(f"revoked {args.id}", file=sys.stderr)
    return 0


# ---------------------------------------------------------------- capacity

def _cmd_capacity(args: argparse.Namespace) -> int:
    """GET /workers/capacity -- the fleet's concurrent-fetch capacity."""
    import httpx  # lazy so --help stays fast
    import json
    hub = _resolve_hub(args.hub)
    try:
        r = httpx.get(
            f"{hub}/workers/capacity", headers=_auth_headers(args), timeout=30,
        )
    except Exception as e:  # noqa: BLE001
        print(f"error: {e}", file=sys.stderr)
        return 1
    if r.status_code != 200:
        print(f"error {r.status_code}: {r.text}", file=sys.stderr)
        return 1
    d = r.json()
    if getattr(args, "quiet", False):
        # just the number, for scripting: N=$(paprika-client capacity -q)
        print(d.get("recommended_concurrency"))
    else:
        print(json.dumps(d, indent=2))
    return 0


# ---------------------------------------------------------------- main

def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(
        prog="paprika-client",
        description="paprika hub CLI helpers (profile upload, ...)",
    )
    p.add_argument(
        "--hub", default=None,
        help="Hub base URL (default: $PAPRIKA_HUB or http://localhost:8000)",
    )
    p.add_argument(
        "--token", default=None,
        help="API key / bearer token (default: $PAPRIKA_API_KEY or "
             "$PAPRIKA_TOKEN). Required once the hub runs auth_mode=enforce.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    up = sub.add_parser(
        "upload-profile",
        help="Snapshot local Chrome + upload to the hub",
    )
    up.add_argument(
        "--name", required=True,
        help="Profile name to register under on the hub (used as "
             "options.use_profile by jobs)",
    )
    up.add_argument(
        "--chrome-profile", default="Default",
        help="Local Chrome profile to snapshot. Default: 'Default'. "
             "Check chrome://version 'Profile Path' for multi-profile setups.",
    )
    up.add_argument(
        "--include", action="append",
        help="Extra per-profile file/dir name to bundle on top of the "
             "defaults (e.g. 'Bookmarks', 'History'). Repeatable.",
    )
    up.add_argument(
        "--note", default=None,
        help="Free-text note shown in the admin UI next to this profile.",
    )
    up.add_argument(
        "--no-decrypt-cookies", action="store_true",
        help="Skip the operator-side cookie decryption step. Use "
             "ONLY when uploading from a Linux desktop to a Linux "
             "worker that share the same Chrome keyring backend "
             "(both fall back to 'peanuts'). Without this flag the "
             "CLI decrypts the OS-bound encrypted_value column and "
             "stashes plaintext in the cookie's value column so the "
             "worker's Linux Chrome can read them.",
    )
    up.set_defaults(func=_cmd_upload_profile)

    ls = sub.add_parser(
        "list-profiles",
        help="List uploaded profiles on the hub",
    )
    ls.set_defaults(func=_cmd_list_profiles)

    rm = sub.add_parser(
        "delete-profile",
        help="Delete an uploaded profile",
    )
    rm.add_argument(
        "--name", required=True,
        help="Profile name to delete",
    )
    rm.set_defaults(func=_cmd_delete_profile)

    sd = sub.add_parser(
        "set-default-profile",
        help="Mark a profile as the auto-applied default (or clear "
             "with no --name). Jobs without options.use_profile then "
             "use this profile.",
    )
    sd.add_argument(
        "--name", default=None,
        help="Profile name to mark as default. Omit to clear the "
             "current default.",
    )
    sd.set_defaults(func=_cmd_set_default_profile)

    # ---- auth: identity + API-key management (LAN-trust → multi-user) ----
    wa = sub.add_parser(
        "auth-whoami",
        help="Show the caller's resolved principal (GET /auth/me)",
    )
    wa.set_defaults(func=_cmd_auth_whoami)

    kc = sub.add_parser(
        "auth-key-create",
        help="Mint an API key; prints the secret ONCE to stdout",
    )
    kc.add_argument("--name", default=None, help="Human label for the key")
    kc.add_argument(
        "--user-id", default=None,
        help="Owner user id (admin/off only; defaults to the calling user)",
    )
    kc.set_defaults(func=_cmd_auth_key_create)

    kl = sub.add_parser(
        "auth-key-list", help="List API keys (own; admin sees all)",
    )
    kl.set_defaults(func=_cmd_auth_key_list)

    kr = sub.add_parser("auth-key-revoke", help="Revoke an API key by id")
    kr.add_argument("id", help="Key id (key_...) to revoke")
    kr.set_defaults(func=_cmd_auth_key_revoke)

    cap = sub.add_parser(
        "capacity",
        help="Fleet concurrent-fetch capacity (max / recommended / available)",
    )
    cap.add_argument(
        "-q", "--quiet", action="store_true",
        help="Print ONLY recommended_concurrency (for scripting)",
    )
    cap.set_defaults(func=_cmd_capacity)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
