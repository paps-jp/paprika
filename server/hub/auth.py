"""Authentication & authorization foundation.

Phase 1 of moving paprika off the "trusted LAN" premise toward a
multi-user, publicly-exposable base. This module provides the identity
primitives; the actual *enforcement* (middleware + route deps) lives in
``app.py`` and only blocks when the auth mode is ``enforce``. The
``off`` (default) and ``optional`` modes are deliberately non-breaking.

What's here:

* :class:`Principal` — the resolved identity of a request
  (user / api-key / anonymous / system / worker).
* :class:`AuthMode` — ``off`` → ``optional`` → ``enforce`` ramp.
* Crypto helpers:
    - **passwords** (UI login, low-frequency, low-entropy) → ``scrypt``.
    - **API keys** (every client request, high-entropy random token) →
      a single ``sha256`` + constant-time compare. A slow KDF is for
      low-entropy secrets; a 256-bit random token doesn't need one, and
      the per-request hot path must stay cheap.
    - **session cookies** → HMAC-SHA256 signed, stateless.
* :class:`AuthStore` — ``users`` + ``api_keys`` persistence with a dual
  backend, chosen at runtime:
    - MariaDB (``state.mariadb_pool``) when configured → durable + shared
      across hubs (the prod multi-hub source of truth).
    - a local JSON file (``{data_dir}/auth/auth.json``) otherwise → dev /
      single-hub fallback.
  Reads are served from an in-memory cache refreshed on a short TTL, so an
  API-key revocation on one hub propagates to every hub within the TTL
  (each hub re-reads the shared MariaDB). Freshly-created keys resolve
  immediately via a cache-miss forced reload.

stdlib only — no new pip dependency (the hub image deliberately ships a
minimal set; see CLAUDE.md).
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from server.hub._jsonstore import atomic_write_json
from server.hub._state import config, state

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Auth mode
# ---------------------------------------------------------------------------

class AuthMode:
    OFF = "off"            # today's behaviour: every request is the SYSTEM principal
    OPTIONAL = "optional"  # attribute when a credential is present, else ANONYMOUS — never blocks
    ENFORCE = "enforce"    # a valid principal is required; anonymous is rejected

    ALL = (OFF, OPTIONAL, ENFORCE)


def current_mode() -> str:
    """The active auth mode (``config.auth_mode``, validated)."""
    m = (getattr(config, "auth_mode", None) or AuthMode.OFF).strip().lower()
    return m if m in AuthMode.ALL else AuthMode.OFF


# ---------------------------------------------------------------------------
# Principal
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Principal:
    """The resolved identity of a request.

    ``kind``:
      * ``system``    — internal / auth-off; treated as admin so authz deps pass.
      * ``user``      — a real account, authenticated via session cookie or API key.
      * ``worker``    — a fleet worker (authenticated by ``worker_secret`` elsewhere).
      * ``anonymous`` — no credential presented (only reachable in off/optional modes).
    """

    kind: str
    id: str = ""
    email: str = ""
    role: str = "user"          # "user" | "admin"
    scopes: tuple[str, ...] = ()
    key_id: str = ""            # set when authenticated via an API key

    @property
    def is_authenticated(self) -> bool:
        return self.kind in ("user", "system", "worker")

    @property
    def is_admin(self) -> bool:
        return self.kind == "system" or (self.kind == "user" and self.role == "admin")

    def to_log(self) -> str:
        if self.kind == "user":
            return f"user:{self.email or self.id}" + (f"#key:{self.key_id}" if self.key_id else "")
        return self.kind


# Sentinels reused everywhere so identity checks are cheap.
SYSTEM = Principal(kind="system", id="system", role="admin")
ANONYMOUS = Principal(kind="anonymous", id="anonymous", role="anon")


# ---------------------------------------------------------------------------
# Crypto helpers
# ---------------------------------------------------------------------------

# scrypt cost params. n=2**14 keeps a login verify well under ~100ms while
# being expensive enough for a low-entropy password. Stored inline in the
# hash string so we can raise them later without breaking old hashes.
_SCRYPT_N = 1 << 14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32


def _b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode("ascii").rstrip("=")


def _b64d(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def hash_password(password: str) -> str:
    """Hash a UI-login password with scrypt. Returns a self-describing
    string ``scrypt$n$r$p$salt$hash`` so verify() needs no side config."""
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=_SCRYPT_DKLEN,
        maxmem=0,
    )
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${_b64e(salt)}${_b64e(dk)}"


def verify_password(password: str, stored: str) -> bool:
    """Constant-time verify against a :func:`hash_password` string."""
    try:
        algo, n_s, r_s, p_s, salt_s, hash_s = stored.split("$")
        if algo != "scrypt":
            return False
        n, r, p = int(n_s), int(r_s), int(p_s)
        salt = _b64d(salt_s)
        expected = _b64d(hash_s)
        dk = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt, n=n, r=r, p=p, dklen=len(expected), maxmem=0,
        )
        return hmac.compare_digest(dk, expected)
    except Exception:
        return False


# API-key wire format: ``pk_<prefix>.<secret>``
#   prefix — 12 hex chars, the public lookup key (indexed, stored in clear)
#   secret — 43-char url-safe random (256 bits), only its sha256 is stored
_KEY_SCHEME = "pk_"


def generate_api_key() -> tuple[str, str, str, str]:
    """Mint a new API key. Returns ``(key_id, prefix, secret_hash, plaintext)``.
    The *plaintext* is shown to the operator exactly once; only ``prefix`` +
    ``secret_hash`` are persisted."""
    key_id = "key_" + secrets.token_hex(8)
    prefix = secrets.token_hex(6)
    secret = secrets.token_urlsafe(32)
    plaintext = f"{_KEY_SCHEME}{prefix}.{secret}"
    return key_id, prefix, hash_api_key_secret(secret), plaintext


def hash_api_key_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def parse_api_key(plaintext: str) -> tuple[str, str] | None:
    """Split ``pk_<prefix>.<secret>`` → ``(prefix, secret)``; None if malformed."""
    if not plaintext or not plaintext.startswith(_KEY_SCHEME):
        return None
    body = plaintext[len(_KEY_SCHEME):]
    if "." not in body:
        return None
    prefix, secret = body.split(".", 1)
    if not prefix or not secret:
        return None
    return prefix, secret


# ---------------------------------------------------------------------------
# Session cookies (stateless, HMAC-signed)
# ---------------------------------------------------------------------------

SESSION_COOKIE = "paprika_session"
_SESSION_TTL_S = 7 * 24 * 3600  # 7 days
_session_secret_cache: bytes | None = None


def get_session_secret() -> bytes:
    """The HMAC key for session cookies.

    Resolution: ``PAPRIKA_SESSION_SECRET`` env (MUST be set + identical on
    every hub in a multi-hub deploy so a cookie minted on one hub validates
    on the next) → else a random secret persisted to
    ``{data_dir}/auth/session_secret`` (fine for dev / single hub)."""
    global _session_secret_cache
    if _session_secret_cache is not None:
        return _session_secret_cache
    env = (os.environ.get("PAPRIKA_SESSION_SECRET") or "").strip()
    if env:
        _session_secret_cache = env.encode("utf-8")
        return _session_secret_cache
    path = Path(config.data_dir) / "auth" / "session_secret"
    try:
        if path.exists():
            _session_secret_cache = path.read_bytes().strip()
            if _session_secret_cache:
                return _session_secret_cache
        path.parent.mkdir(parents=True, exist_ok=True)
        secret = secrets.token_bytes(32)
        path.write_bytes(secret)
        log.warning(
            "auth: PAPRIKA_SESSION_SECRET unset — generated an ephemeral "
            "per-hub session secret at %s. Set the env (same value on every "
            "hub) before relying on UI login in a multi-hub deploy.", path,
        )
        _session_secret_cache = secret
        return _session_secret_cache
    except Exception:
        # Last resort: a process-lifetime random key (sessions won't
        # survive a restart, but login still works within this process).
        _session_secret_cache = secrets.token_bytes(32)
        return _session_secret_cache


def sign_session(payload: dict, *, ttl_s: int = _SESSION_TTL_S) -> str:
    body = dict(payload)
    body["exp"] = int(time.time()) + ttl_s
    raw = json.dumps(body, separators=(",", ":"), default=str).encode("utf-8")
    b = _b64e(raw)
    mac = hmac.new(get_session_secret(), b.encode("ascii"), hashlib.sha256).digest()
    return f"{b}.{_b64e(mac)}"


def verify_session(token: str) -> dict | None:
    """Verify + decode a session cookie. None if tampered / expired."""
    try:
        b, mac_s = token.split(".", 1)
        expected = hmac.new(get_session_secret(), b.encode("ascii"), hashlib.sha256).digest()
        if not hmac.compare_digest(_b64d(mac_s), expected):
            return None
        payload = json.loads(_b64d(b))
        if int(payload.get("exp", 0)) < int(time.time()):
            return None
        return payload
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Datetime helpers
# ---------------------------------------------------------------------------

def _utcnow_iso() -> str:
    return datetime.utcnow().isoformat() + "Z"


def _parse_iso(v: Any) -> float | None:
    """ISO string / datetime → epoch seconds, or None."""
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.timestamp()
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        s = v.strip().rstrip("Z")
        if not s:
            return None
        try:
            return datetime.fromisoformat(s).timestamp()
        except ValueError:
            return None
    return None


# ---------------------------------------------------------------------------
# AuthStore
# ---------------------------------------------------------------------------

@dataclass
class _UserRec:
    id: str
    email: str
    pw_hash: str
    role: str = "user"
    disabled: bool = False
    created_at: str = ""

    def public(self) -> dict:
        return {
            "id": self.id, "email": self.email, "role": self.role,
            "disabled": self.disabled, "created_at": self.created_at,
        }


@dataclass
class _KeyRec:
    id: str
    prefix: str
    secret_hash: str
    user_id: str
    name: str = ""
    scopes: tuple[str, ...] = ()
    created_at: str = ""
    last_used_at: str = ""
    expires_at: str = ""
    revoked: bool = False

    def public(self) -> dict:
        """Redacted view — never exposes ``secret_hash``."""
        return {
            "id": self.id, "prefix": self.prefix, "user_id": self.user_id,
            "name": self.name, "scopes": list(self.scopes),
            "created_at": self.created_at, "last_used_at": self.last_used_at,
            "expires_at": self.expires_at, "revoked": self.revoked,
        }


class AuthStore:
    """Users + API keys, MariaDB-or-file backed with an in-memory cache."""

    def __init__(self, data_dir: Path | str, *, cache_ttl_s: float = 30.0) -> None:
        self._file = Path(data_dir) / "auth" / "auth.json"
        self._cache_ttl = cache_ttl_s
        self._users: dict[str, _UserRec] = {}
        self._users_by_email: dict[str, str] = {}
        self._keys: dict[str, _KeyRec] = {}  # by prefix
        self._loaded_at: float = 0.0
        self._loaded_backend: str = ""
        self._write_lock: asyncio.Lock | None = None

    # -- backend selection --------------------------------------------------

    def _pool(self):
        """Current MariaDB pool, or None → file backend."""
        return state.mariadb_pool

    def _lock(self) -> asyncio.Lock:
        if self._write_lock is None:
            self._write_lock = asyncio.Lock()
        return self._write_lock

    # -- loading / cache ----------------------------------------------------

    async def _ensure_loaded(self, *, force: bool = False) -> None:
        if not force and (time.monotonic() - self._loaded_at) < self._cache_ttl:
            return
        pool = self._pool()
        try:
            if pool is not None:
                await self._load_mariadb(pool)
                self._loaded_backend = "mariadb"
            else:
                self._load_file()
                self._loaded_backend = "file"
            self._loaded_at = time.monotonic()
        except Exception:
            log.warning("auth: cache reload failed (%s)", self._loaded_backend or "?", exc_info=True)

    def _index(self, users: list[_UserRec], keys: list[_KeyRec]) -> None:
        self._users = {u.id: u for u in users}
        self._users_by_email = {u.email.lower(): u.id for u in users}
        self._keys = {k.prefix: k for k in keys}

    def _load_file(self) -> None:
        data: dict = {}
        if self._file.exists():
            try:
                data = json.loads(self._file.read_text(encoding="utf-8")) or {}
            except Exception:
                data = {}
        users = [_UserRec(**u) for u in data.get("users", [])]
        keys = [
            _KeyRec(**{**k, "scopes": tuple(k.get("scopes", []))})
            for k in data.get("api_keys", [])
        ]
        self._index(users, keys)

    def _persist_file(self) -> None:
        self._file.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(self._file, {
            "users": [u.__dict__ for u in self._users.values()],
            "api_keys": [
                {**k.__dict__, "scopes": list(k.scopes)} for k in self._keys.values()
            ],
        })

    async def _load_mariadb(self, pool) -> None:
        import server.hub.mariadb as mariadb
        await mariadb.ensure_auth_tables(pool)
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT id, email, pw_hash, role, disabled, created_at FROM users"
                )
                urows = await cur.fetchall()
                await cur.execute(
                    "SELECT id, prefix, secret_hash, user_id, name, scopes, "
                    "created_at, last_used_at, expires_at, revoked FROM api_keys"
                )
                krows = await cur.fetchall()
        users = [
            _UserRec(
                id=r[0], email=r[1], pw_hash=r[2], role=r[3] or "user",
                disabled=bool(r[4]), created_at=str(r[5]) if r[5] else "",
            )
            for r in urows
        ]
        keys = [
            _KeyRec(
                id=r[0], prefix=r[1], secret_hash=r[2], user_id=r[3],
                name=r[4] or "", scopes=tuple(json.loads(r[5]) if r[5] else []),
                created_at=str(r[6]) if r[6] else "",
                last_used_at=str(r[7]) if r[7] else "",
                expires_at=str(r[8]) if r[8] else "",
                revoked=bool(r[9]),
            )
            for r in krows
        ]
        self._index(users, keys)

    # -- writes -------------------------------------------------------------

    async def _write_user(self, u: _UserRec) -> None:
        self._users[u.id] = u
        self._users_by_email[u.email.lower()] = u.id
        pool = self._pool()
        if pool is not None:
            import server.hub.mariadb as mariadb
            await mariadb.ensure_auth_tables(pool)
            async with pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "INSERT INTO users (id, email, pw_hash, role, disabled, created_at) "
                        "VALUES (%s,%s,%s,%s,%s,%s) "
                        "ON DUPLICATE KEY UPDATE email=VALUES(email), pw_hash=VALUES(pw_hash), "
                        "role=VALUES(role), disabled=VALUES(disabled)",
                        (u.id, u.email, u.pw_hash, u.role, 1 if u.disabled else 0,
                         _parse_dt_for_sql(u.created_at)),
                    )
        else:
            self._persist_file()

    async def _write_key(self, k: _KeyRec) -> None:
        self._keys[k.prefix] = k
        pool = self._pool()
        if pool is not None:
            import server.hub.mariadb as mariadb
            await mariadb.ensure_auth_tables(pool)
            async with pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "INSERT INTO api_keys (id, prefix, secret_hash, user_id, name, "
                        "scopes, created_at, last_used_at, expires_at, revoked) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                        "ON DUPLICATE KEY UPDATE name=VALUES(name), scopes=VALUES(scopes), "
                        "last_used_at=VALUES(last_used_at), expires_at=VALUES(expires_at), "
                        "revoked=VALUES(revoked)",
                        (k.id, k.prefix, k.secret_hash, k.user_id, k.name,
                         json.dumps(list(k.scopes)),
                         _parse_dt_for_sql(k.created_at),
                         _parse_dt_for_sql(k.last_used_at),
                         _parse_dt_for_sql(k.expires_at),
                         1 if k.revoked else 0),
                    )
        else:
            self._persist_file()

    # -- public API: users --------------------------------------------------

    async def count_users(self) -> int:
        await self._ensure_loaded(force=True)
        return len(self._users)

    async def get_user_by_email(self, email: str) -> _UserRec | None:
        await self._ensure_loaded()
        uid = self._users_by_email.get((email or "").lower())
        return self._users.get(uid) if uid else None

    async def create_user(self, email: str, password: str, *, role: str = "user") -> dict:
        email = (email or "").strip()
        if not email or not password:
            raise ValueError("email and password are required")
        async with self._lock():
            await self._ensure_loaded(force=True)
            if (email.lower()) in self._users_by_email:
                raise ValueError(f"user already exists: {email}")
            u = _UserRec(
                id="usr_" + secrets.token_hex(8),
                email=email, pw_hash=hash_password(password),
                role=role if role in ("user", "admin") else "user",
                disabled=False, created_at=_utcnow_iso(),
            )
            await self._write_user(u)
        return u.public()

    async def verify_login(self, email: str, password: str) -> Principal | None:
        u = await self.get_user_by_email(email)
        if u is None or u.disabled:
            return None
        if not verify_password(password, u.pw_hash):
            return None
        return Principal(kind="user", id=u.id, email=u.email, role=u.role)

    async def bootstrap_admin(self, email: str, password: str) -> bool:
        """Create the first admin from env if the store has no users yet.
        Idempotent; returns True iff it created the account."""
        if not email or not password:
            return False
        async with self._lock():
            await self._ensure_loaded(force=True)
            if self._users:
                return False
            u = _UserRec(
                id="usr_" + secrets.token_hex(8),
                email=email.strip(), pw_hash=hash_password(password),
                role="admin", disabled=False, created_at=_utcnow_iso(),
            )
            await self._write_user(u)
        log.warning("auth: bootstrapped initial admin user %s", email)
        return True

    async def list_users(self) -> list[dict]:
        await self._ensure_loaded()
        out = [u.public() for u in self._users.values()]
        out.sort(key=lambda r: r.get("created_at", ""))
        return out

    # -- public API: API keys ----------------------------------------------

    async def create_api_key(
        self, user_id: str, *, name: str = "", scopes: tuple[str, ...] = (),
        expires_at: str = "",
    ) -> tuple[dict, str]:
        """Mint a key for ``user_id``. Returns ``(public_record, plaintext)``;
        the plaintext is the ONLY time the full key is available."""
        async with self._lock():
            await self._ensure_loaded(force=True)
            if user_id not in self._users:
                raise ValueError("unknown user")
            key_id, prefix, secret_hash, plaintext = generate_api_key()
            k = _KeyRec(
                id=key_id, prefix=prefix, secret_hash=secret_hash, user_id=user_id,
                name=name or "", scopes=tuple(scopes), created_at=_utcnow_iso(),
                expires_at=expires_at or "", revoked=False,
            )
            await self._write_key(k)
        return k.public(), plaintext

    async def list_api_keys(self, user_id: str | None = None) -> list[dict]:
        await self._ensure_loaded()
        out = [
            k.public() for k in self._keys.values()
            if user_id is None or k.user_id == user_id
        ]
        out.sort(key=lambda r: r.get("created_at", ""), reverse=True)
        return out

    async def revoke_api_key(self, key_id: str, *, user_id: str | None = None) -> bool:
        async with self._lock():
            await self._ensure_loaded(force=True)
            target = next((k for k in self._keys.values() if k.id == key_id), None)
            if target is None:
                return False
            if user_id is not None and target.user_id != user_id:
                return False
            target.revoked = True
            await self._write_key(target)
        return True

    async def resolve_api_key(self, plaintext: str) -> Principal | None:
        """Hot path: resolve a presented ``Authorization: Bearer`` key to a
        Principal, or None. Reads the in-memory cache; a cache miss on the
        prefix forces one reload so a key created seconds ago still works."""
        parsed = parse_api_key(plaintext)
        if parsed is None:
            return None
        prefix, secret = parsed
        await self._ensure_loaded()
        rec = self._keys.get(prefix)
        if rec is None:
            await self._ensure_loaded(force=True)
            rec = self._keys.get(prefix)
        if rec is None or rec.revoked:
            return None
        exp = _parse_iso(rec.expires_at)
        if exp is not None and exp < time.time():
            return None
        if not hmac.compare_digest(hash_api_key_secret(secret), rec.secret_hash):
            return None
        u = self._users.get(rec.user_id)
        if u is None or u.disabled:
            return None
        # Throttled last-used stamp (best effort; never blocks the request).
        self._maybe_touch(rec)
        return Principal(
            kind="user", id=u.id, email=u.email, role=u.role,
            scopes=rec.scopes, key_id=rec.id,
        )

    def _maybe_touch(self, rec: _KeyRec) -> None:
        """Update last_used at most once/minute, off the request path."""
        now = time.time()
        last = _parse_iso(rec.last_used_at) or 0.0
        if now - last < 60:
            return
        rec.last_used_at = _utcnow_iso()
        try:
            asyncio.get_running_loop().create_task(self._write_key(rec))
        except Exception:
            pass


def _parse_dt_for_sql(iso: str):
    """ISO string → naive datetime for aiomysql, or None."""
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso.rstrip("Z"))
    except ValueError:
        return None


__all__ = [
    "AuthMode", "current_mode", "Principal", "SYSTEM", "ANONYMOUS",
    "AuthStore", "hash_password", "verify_password",
    "generate_api_key", "hash_api_key_secret", "parse_api_key",
    "SESSION_COOKIE", "sign_session", "verify_session", "get_session_secret",
]
