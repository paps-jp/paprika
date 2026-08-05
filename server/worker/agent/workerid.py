"""Worker-id resolution (IP-derived, stable across restarts). (worker agent package; shared bits in _base.py)."""

from __future__ import annotations
import asyncio
import functools
import json
import os
import random
import shutil
import socket
import logging
import string
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit, urlunsplit
import httpx
from core.httpclient import make_async_client
import websockets
from websockets.exceptions import ConnectionClosed, WebSocketException
from core.fetcher import (
    FetchOptions,
    clone_chrome_profile,
    fetch,
)
from server.protocol import (
    AssetInfo,
    HubAssignJob,
    HubExpectedVersion,
    HubProfileDelete,
    HubProfileSync,
    HubRegistered,
    HubPreviewSubscribe,
    HubScreenshotRequest,
    HubSessionAction,
    HubSessionAgent,
    HubSessionEnd,
    HubSessionInteraction,
    HubSessionStart,
    HubUpdateGate,
    JobOptions,
    JobResult,
    JobStatus,
    ProfileCacheEntry,
    SessionStateSnapshot,
    WorkerCapabilities,
    WorkerDraining,
    WorkerHeartbeat,
    WorkerJobAccepted,
    WorkerJobComplete,
    WorkerJobFailed,
    ASSET_CAPTURE_MARKER,
    JOB_PROGRESS_MARKER,
    LINKS_CAPTURE_MARKER,
    NET_CAPTURE_MARKER,
    WorkerJobLog,
    WorkerJobProgress,
    WorkerRegister,
    WorkerPreviewFrame,
    WorkerScreenshotReply,
    WorkerSessionActionResult,
    WorkerSessionAgentResult,
    WorkerSessionAnnounce,
    WorkerSessionEndAck,
    WorkerSessionStartAck,
    YtdlpResult,
    decode_hub_msg,
    encode_msg,
)
from server.scheduler import HEARTBEAT_INTERVAL
from server.worker import browser_ops
from server.worker.sessions import SessionState
from server.worker._browser_helpers import (
    _LINKS_EXTRACT_JS,
    _VIDEO_DIRECT_RE,
    _VIDEO_STREAM_RE,
    _evaluate_in_frame,
    _looks_like_player_iframe,
)
from server.worker.session_actions import (
    _ActionCtx,
    _SESSION_ACTIONS,
)
import re as _re

from ._base import *  # noqa: F401,F403

def _resolve_worker_id_file() -> Path:
    """Cross-platform default for the worker_id persistence file.

    Resolution order:

      1. ``PAPRIKA_WORKER_ID_FILE`` env var — explicit override. Use this
         when you want the worker_id to land in an unusual place (e.g.
         a host-mounted Windows path under Docker Desktop, or a shared
         network filesystem). The directory is created on demand.

      2. ``~/.paprika/worker_id`` — the historical default. Resolves to::

           Linux container:   /root/.paprika/worker_id   (default $HOME)
           Linux native:      /home/<user>/.paprika/worker_id
           macOS:             /Users/<user>/.paprika/worker_id
           Windows native:    C:\\Users\\<user>\\.paprika\\worker_id

         The docker-compose worker service mounts ``paprika-worker-state``
         at ``/root/.paprika`` so this path survives container restarts.

      3. ``<tempdir>/paprika/worker_id`` — fallback when ``Path.home()``
         is unusable (rare Windows service contexts, restricted Docker
         runtimes). Survives the process but not a host reboot.

      4. ``./.paprika/worker_id`` — last resort, relative to CWD.
    """
    env = os.environ.get("PAPRIKA_WORKER_ID_FILE", "").strip()
    if env:
        return Path(env)
    try:
        home = Path.home()
        # Path.home() can return Path("/") or similar nonsense under
        # some service-account / minimal-env Docker configurations; only
        # honor it if it points somewhere with depth.
        if str(home) not in ("", "/", "\\", ".") and home.parent != home:
            return home / ".paprika" / "worker_id"
    except Exception:
        pass
    try:
        import tempfile as _tempfile

        return Path(_tempfile.gettempdir()) / "paprika" / "worker_id"
    except Exception:
        pass
    return Path(".paprika") / "worker_id"


WORKER_ID_FILE = _resolve_worker_id_file()


class _WorkerIdReassigned(Exception):
    """Raised when the hub instructs this worker to adopt a fresh ID.

    The hub detects clone collisions (same persisted ``worker_id`` arriving
    from a different client IP than the still-alive original) and replies
    via ``HubRegistered.assigned_worker_id``. We catch this in the outer
    reconnect loop in :meth:`WorkerAgent.run` so the next attempt dials
    the link URL with the freshly-persisted ID.
    """


#: Container-private ranges that are NEVER this CT's LAN identity: docker
#: bridge (172.16/12), loopback, link-local, and the unspecified address.
#: Deriving a worker_id from one of these is what broke the fleet on
#: 2026-08-05 -- see usable_lan_ip().
def usable_lan_ip(ip: str | None) -> str:
    """*ip* if it is this CT's real LAN address, else "".

    The worker container sits on a docker bridge, so the kernel's route to the
    hub has source ``172.18.0.2`` on EVERY worker in the fleet. Deriving the id
    from it gave all ~100 workers the same ``w02``; they then collided on the
    node-shared Chrome ramdisk (``/var/paprika/chrome/<worker_id>/``), each
    tripping over the previous CT's Chrome ``SingletonLock`` -> lane start
    failed -> crash loop on 63 of 88 workers (2026-08-05). Reject the ranges a
    container invents for itself so we fall through to an authoritative source
    instead of inventing a colliding identity.
    """
    ip = (ip or "").strip()
    parts = ip.split(".")
    if len(parts) != 4 or not all(p.isdigit() and 0 <= int(p) <= 255 for p in parts):
        return ""
    o = [int(p) for p in parts]
    if o[0] in (0, 127):                      # unspecified / loopback
        return ""
    if o[0] == 169 and o[1] == 254:           # link-local
        return ""
    if o[0] == 172 and 16 <= o[1] <= 31:      # docker bridge pools
        return ""
    return ip


def lan_ip_via_hub(hub_url: str = "", *, timeout_s: float = 0.0, attempts: int = 3) -> str:
    """This CT's LAN IP as the HUB sees it, or "".

    The hub is on the other side of the CT's NAT, so its view of our source
    address IS the CT's LAN IP (``10.10.51.145``) -- the one thing the
    container cannot work out on its own. ``GET /health`` carries it in
    ``client_ip``: no new endpoint, reachable from any hub, and available at
    the earliest point of worker init (before the lane pool, which is what
    consumes the id). See server/hub/routes/system.py:caller_ip.

    Best-effort by design: a hub that is mid-restart must not block worker
    startup, so this gives up after a few short attempts and the caller falls
    back. Env: ``PAPRIKA_WORKER_ID_HUB_PROBE_DISABLE=1`` skips it entirely,
    ``PAPRIKA_WORKER_ID_HUB_PROBE_TIMEOUT_S`` tunes the per-try timeout
    (default 3s).
    """
    if os.environ.get("PAPRIKA_WORKER_ID_HUB_PROBE_DISABLE"):
        return ""
    raw = (hub_url or os.environ.get("HUB_URL") or "").strip()
    if not raw:
        return ""
    if timeout_s <= 0:
        try:
            timeout_s = float(
                (os.environ.get("PAPRIKA_WORKER_ID_HUB_PROBE_TIMEOUT_S") or "").strip()
                or 3.0
            )
        except ValueError:
            timeout_s = 3.0
    base = hub_http_base(raw)
    for attempt in range(1, max(1, attempts) + 1):
        try:
            r = httpx.get(f"{base}/health", timeout=timeout_s)
            if r.status_code == 200:
                ip = usable_lan_ip((r.json() or {}).get("client_ip"))
                if ip:
                    return ip
                # A hub that answers without a usable client_ip (old build, or
                # a dev stack where hub and worker share one bridge) is not
                # going to start answering on a retry.
                return ""
        except Exception:
            pass
        if attempt < attempts:
            time.sleep(0.5 * attempt)
    return ""


def lan_ip() -> str:
    """This host's IP on the route towards the hub.

    A UDP "connect" performs no I/O -- it only makes the kernel pick the
    source address it *would* use -- so this is instant, needs no traffic and
    picks the right interface on a multi-homed box (a worker CT has its LAN
    veth plus docker's bridges, and ``gethostbyname(gethostname())`` happily
    returns a docker address).

    LIMIT: this is the route as seen from wherever we run. INSIDE the worker
    container every route to the hub leaves via the docker bridge, so this
    returns ``172.18.0.2`` -- the same value on every worker in the fleet, and
    NOT this CT's identity. Callers must filter through ``usable_lan_ip()``
    and prefer ``lan_ip_via_hub()``.
    """
    target = ""
    try:
        raw = (os.environ.get("HUB_URL") or "").strip()
        if raw:
            host = urlsplit(raw).hostname
            if host:
                target = host
    except Exception:
        pass
    for dest in (target, "10.255.255.255", "8.8.8.8"):
        if not dest:
            continue
        s = None
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(0.5)
            s.connect((dest, 9))
            ip = s.getsockname()[0]
            if ip and not ip.startswith("127."):
                return ip
        except Exception:
            continue
        finally:
            if s is not None:
                try:
                    s.close()
                except Exception:
                    pass
    return ""


def worker_id_from_ip(ip: str) -> str:
    """``10.10.51.15`` -> ``w5115``.

    Third and fourth octet concatenated, matching the IDs this fleet has used
    since the provisioning scripts started stamping them into ``.env`` -- the
    point of deriving it here is that every worker computes the SAME id it
    already has, so nothing is renamed when this ships.

    NOTE the format is ambiguous by construction: 10.10.51.12 and 10.10.5.112
    both give ``w5112``. Harmless while the fleet lives in a single /24 worth
    of third octet, but a supernet that spans 10.10.48-55 can collide. Any fix
    changes every existing id, so it belongs to a deliberate renumbering, not
    to this change.
    """
    parts = ip.split(".")
    if len(parts) != 4 or not all(p.isdigit() for p in parts):
        return ""
    return f"w{parts[2]}{parts[3]}"


def default_worker_id(hub_url: str = "") -> str:
    """Auto-generate (or recall) a worker ID.

    Resolution order:

      1. **Derived from this CT's LAN IP as the HUB reports it**
         (``10.10.51.15`` -> ``w5115``). Authoritative: the hub sits outside
         the CT's NAT, so it sees the address that actually identifies this
         box. Deterministic, needs no state, and -- crucially -- a CLONED CT
         gets a new id the moment it gets a new IP. Deriving beats recalling
         here: a clone carries the source's ``paprika-worker-state`` volume, so
         a persisted id would follow it and two workers would fight over one
         identity until the hub's collision detector broke the tie.
      2. Derived from a LOCALLY observed LAN IP -- only when it is a real one.
         Covers a worker running outside a container (native / host network)
         and keeps this working with no hub reachable. A container-private
         address (docker bridge) is REJECTED here: it is identical on every
         worker in the fleet, so it produces a colliding id, not an identity
         (see usable_lan_ip -- this is the 2026-08-05 crash-loop bug).
      3. The persisted ``~/.paprika/worker_id`` -- for hosts where both probes
         fail, and for a container that has already been told its stable id by
         the hub (it persists the reassignment).
      4. ``<hostname>-<rand4>``, persisted for next time. Random, but unique
         per container -- so a fleet that lands here degrades to "ids churn on
         recreate", never to "every worker claims the same identity".

    Deliberately NOT persisted in cases 1-2: writing it back would re-create
    the per-container state this exists to remove, and a stale file would then
    outrank a changed IP on the next boot.
    """
    derived = worker_id_from_ip(lan_ip_via_hub(hub_url))
    if derived:
        return derived

    derived = worker_id_from_ip(usable_lan_ip(lan_ip()))
    if derived:
        return derived

    try:
        if WORKER_ID_FILE.exists():
            persisted = WORKER_ID_FILE.read_text().strip()
            if persisted:
                return persisted
    except Exception:
        pass

    host = socket.gethostname()
    suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=4))
    nid = f"{host}-{suffix}"

    try:
        WORKER_ID_FILE.parent.mkdir(parents=True, exist_ok=True)
        WORKER_ID_FILE.write_text(nid)
    except Exception:
        pass
    return nid


def hub_http_base(ws_url: str) -> str:
    """Convert ws:// -> http://, wss:// -> https://."""
    parts = urlsplit(ws_url)
    scheme = {"ws": "http", "wss": "https"}.get(parts.scheme, parts.scheme)
    new = parts._replace(scheme=scheme)
    return urlunsplit(new).rstrip("/")

