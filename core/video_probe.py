"""No-browser HLS/DASH reachability classifier (measurement "E", 2026-07-08).

Right after the network sniffer captures a video manifest, we fire a one-shot,
no-live-Chrome reachability probe using the SAME transport the worker's actual
download uses (``core.httpclient.make_async_client`` -> worker egress proxy +
TLS/DNS knobs). The verdict tells the hub, per host, what fraction of video
streams are downloadable WITHOUT holding a Chrome lane -- the feasibility
signal for splitting video downloads onto a Chrome-less download tier.

PURE TELEMETRY. Every entry point is wrapped so a probe failure can never
affect the real download. Kill-switch: ``PAPRIKA_VIDEO_PROBE=0``.

Verdict taxonomy (the ``verdict`` field):
  decouplable : manifest + first segment (+ key) all fetched OK, no token/
                expiry hint -> a Chrome-less tier can download it as-is.
  tokened     : segment fetched OK but the URL carries a token/expiry param
                (or the stream is live) -> works for a fast/short pull, may
                need a replicated keepalive for a long one.
  gated       : segment or key 401/403 even right after capture -> needs the
                live browser session / a replicated keepalive NOW.
  drm         : SAMPLE-AES / Widevine signalled -> yt-dlp can't take it.
  error       : probe couldn't run (network / parse) -> excluded from stats.
"""
from __future__ import annotations

import asyncio
import os
import re
from urllib.parse import urljoin, urlparse

# A short-lived, per-session token embedded in a manifest/segment/key URL is
# the tell-tale of a stream whose delivery is gated on the live browser
# session (the keepalive-poll design). Heuristic over common CDN param names.
_TOKEN_HINT_RE = re.compile(
    r"(?:^|[?&])(?:token|expires?|expiry|st|e|hash|sig|signature|key|policy|"
    r"md5|hdnts|hmac|__hdnea__|auth|ttl|validfrom|validto)=",
    re.I,
)
# A master (multivariant) playlist declares variants with #EXT-X-STREAM-INF,
# each followed by the variant URI on the next non-comment line.
_STREAM_INF_RE = re.compile(r"^#EXT-X-STREAM-INF:[^\n]*\n([^\s#][^\n]*)", re.M)
_KEY_RE = re.compile(r"^#EXT-X-KEY:([^\n]*)", re.M)
_ENDLIST_RE = re.compile(r"^#EXT-X-ENDLIST", re.M)

# Fire-and-forget probe tasks, held until done so the loop doesn't GC them
# mid-flight (and emit "Task was destroyed but it is pending" warnings).
_inflight: set = set()
# Async sink (set once by the worker agent) that ships a verdict dict to the
# hub as a WorkerVideoProbe. None until the agent registers it.
_sink = None


def set_probe_sink(async_fn) -> None:
    """Worker agent registers its WS emitter here once at startup."""
    global _sink
    _sink = async_fn


def host_of(url: str) -> str:
    """registrable-ish host key: lowercased hostname, ``www.`` stripped."""
    try:
        h = (urlparse(url).hostname or "").lower()
        return h[4:] if h.startswith("www.") else h
    except Exception:
        return ""


def _parse_key(key_line: str) -> tuple[str, str]:
    """(enc_method, key_uri) from an #EXT-X-KEY attribute list."""
    m = re.search(r"METHOD=([A-Za-z0-9\-]+)", key_line or "")
    method = (m.group(1) if m else "").upper()
    u = re.search(r'URI="([^"]+)"', key_line or "")
    key_uri = u.group(1) if u else ""
    if method in ("", "NONE"):
        return "none", ""
    if method == "AES-128":
        return "aes-128", key_uri
    if method.startswith("SAMPLE-AES"):
        return "sample-aes", key_uri
    return method.lower(), key_uri


async def classify_stream(
    manifest_url: str, *, referer, client, timeout_s: float = 8.0,
) -> dict:
    """No-browser reachability classify for one HLS/DASH manifest.

    ``client`` is an httpx-style async client (make_async_client) already
    bound to the worker's egress proxy + TLS/DNS -- so the verdict matches
    what a Chrome-less download tier would actually get. Never raises."""
    out = {
        "manifest_url": manifest_url,
        "verdict": "error",
        "enc_method": "none",
        "is_live": False,
        "token_hint": bool(_TOKEN_HINT_RE.search(manifest_url or "")),
        "manifest_status": 0,
        "segment_status": 0,
        "key_status": 0,
        "tls": "async-client",
    }
    hdrs = {"Referer": referer} if referer else {}
    try:
        base = (manifest_url or "").split("?", 1)[0].lower()
        # DASH: skip the per-segment HLS parse; manifest reachability is the
        # signal (a minority of targets; keeps the probe simple + cheap).
        if base.endswith(".mpd"):
            r = await client.get(manifest_url, headers=hdrs, timeout=timeout_s)
            sc = int(getattr(r, "status_code", 0) or 0)
            out["manifest_status"] = sc
            out["verdict"] = (
                "decouplable" if 200 <= sc < 300
                else "gated" if sc in (401, 403)
                else "error"
            )
            return out

        r = await client.get(manifest_url, headers=hdrs, timeout=timeout_s)
        out["manifest_status"] = int(getattr(r, "status_code", 0) or 0)
        body = r.text or ""
        if not (200 <= out["manifest_status"] < 300) or not body:
            out["verdict"] = (
                "gated" if out["manifest_status"] in (401, 403) else "error"
            )
            return out
        out["is_live"] = _ENDLIST_RE.search(body) is None

        # Follow one master -> media hop to reach a real segment/key.
        media_url, media_body = manifest_url, body
        mv = _STREAM_INF_RE.search(body)
        if mv:
            variant = urljoin(manifest_url, mv.group(1).strip())
            rv = await client.get(variant, headers=hdrs, timeout=timeout_s)
            if 200 <= int(getattr(rv, "status_code", 0) or 0) < 300 and (rv.text or ""):
                media_url, media_body = variant, rv.text
                out["is_live"] = _ENDLIST_RE.search(media_body) is None
                if _TOKEN_HINT_RE.search(variant):
                    out["token_hint"] = True

        # Encryption method (AES-128 = still downloadable by yt-dlp if the
        # key URI is reachable; SAMPLE-AES = DRM = not downloadable).
        km = _KEY_RE.search(media_body)
        key_uri = ""
        if km:
            out["enc_method"], key_uri = _parse_key(km.group(1))
        if out["enc_method"] == "sample-aes":
            out["verdict"] = "drm"
            return out

        # First media segment.
        seg = None
        for line in media_body.splitlines():
            s = line.strip()
            if s and not s.startswith("#"):
                seg = urljoin(media_url, s)
                break
        if seg and _TOKEN_HINT_RE.search(seg):
            out["token_hint"] = True

        # Fetch the key (tiny) + first 2 bytes of the segment -- we only want
        # the status code, not the bytes, so a Range keeps it feather-light.
        if key_uri:
            try:
                rk = await client.get(
                    urljoin(media_url, key_uri), headers=hdrs, timeout=timeout_s,
                )
                out["key_status"] = int(getattr(rk, "status_code", 0) or 0)
            except Exception:
                out["key_status"] = 0
        if seg:
            try:
                rs = await client.get(
                    seg, headers={**hdrs, "Range": "bytes=0-1"}, timeout=timeout_s,
                )
                out["segment_status"] = int(getattr(rs, "status_code", 0) or 0)
            except Exception:
                out["segment_status"] = 0

        seg_ok = 200 <= out["segment_status"] < 400
        key_ok = (not key_uri) or (200 <= out["key_status"] < 400)
        if out["segment_status"] in (401, 403) or out["key_status"] in (401, 403):
            out["verdict"] = "gated"
        elif seg_ok and key_ok:
            out["verdict"] = "tokened" if (out["is_live"] or out["token_hint"]) else "decouplable"
        else:
            out["verdict"] = "error"
    except Exception:
        out["verdict"] = "error"
    return out


async def _probe_and_emit(manifest_url: str, referer: str, page_host: str) -> None:
    try:
        from core.httpclient import make_async_client
        async with make_async_client(timeout=10.0, follow_redirects=True) as client:
            res = await classify_stream(manifest_url, referer=referer, client=client)
        res["host"] = page_host
        if _sink is not None:
            await _sink(res)
    except Exception:
        pass


def spawn_probes(targets, page_url: str) -> None:
    """Fire-and-forget one classify task per (url, referer, label) target.

    ``targets`` = the worker's ``ytdlp_targets`` list of (url, referer, label)
    tuples. Best-effort + kill-switchable; never raises into the caller."""
    if os.environ.get("PAPRIKA_VIDEO_PROBE", "1") == "0":
        return
    try:
        host = host_of(page_url)
        for t in targets or []:
            try:
                u = t[0]
                ref = t[1] if len(t) > 1 else ""
            except Exception:
                continue
            if not u:
                continue
            try:
                task = asyncio.create_task(_probe_and_emit(u, ref or "", host))
                _inflight.add(task)
                task.add_done_callback(_inflight.discard)
            except Exception:
                pass
    except Exception:
        pass
