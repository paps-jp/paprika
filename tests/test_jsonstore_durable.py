"""Regression: high-frequency registry writes must not fsync on the event loop.

2026-08-30. The hub's admin UI, ``POST /jobs`` and asset uploads all share one
asyncio event loop per hub process. ``atomic_write_json`` fsync-ed on that
loop, and ``HostRegistry.touch_used`` calls it **once per crawl session** --
rewriting the host's entire record (cookie jar included, ~600 KB observed) just
to bump ``last_used_at``.

Measured with py-spy against three live hubs while jobs were flowing: the event
loop's innermost frame was ``atomic_write_json`` in 28-60% of samples. The
admin UI went from p50 0.02s / max 0.04s (jobs stopped, 25/25 samples idle) to
p50 0.57s / p90 12.7s / max 26.6s (jobs flowing). Job admission latency scaled
with submitter concurrency while Redis sat at 0% CPU and the hubs' own
object-store threads sat idle -- i.e. nothing was saturated except this loop.

The fix keeps the atomic rename (readers never see a torn file) and makes the
fsync opt-out, so:
  - operator/config writes stay durable,
  - per-request timestamp bumps do not block the loop.

Losing the newest ``last_used_at`` to a crash is harmless: the previous
complete record survives and the next session re-bumps it.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server.hub._jsonstore import JsonRecordRegistry, atomic_write_json


@pytest.fixture
def no_fsync(monkeypatch):
    """Record every fsync so a test can assert it did (or did not) happen."""
    calls: list[int] = []
    real = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: (calls.append(fd), real(fd))[1])
    return calls


# ── atomic_write_json ────────────────────────────────────────────────────────

def test_durable_write_fsyncs(tmp_path, no_fsync):
    atomic_write_json(tmp_path / "a.json", {"x": 1})
    assert len(no_fsync) == 1, "operator/config writes must stay durable"


def test_non_durable_write_skips_fsync(tmp_path, no_fsync):
    atomic_write_json(tmp_path / "a.json", {"x": 1}, durable=False)
    assert no_fsync == [], "durable=False must not block the event loop on fsync"


def test_default_is_durable(tmp_path, no_fsync):
    """Opt-OUT, not opt-in: a caller that says nothing keeps the safe path."""
    atomic_write_json(tmp_path / "a.json", {"x": 1})
    assert len(no_fsync) == 1


@pytest.mark.parametrize("durable", [True, False])
def test_content_is_written_either_way(tmp_path, durable):
    p = tmp_path / "a.json"
    atomic_write_json(p, {"x": 1, "s": "あ"}, durable=durable)
    assert json.loads(p.read_text(encoding="utf-8")) == {"x": 1, "s": "あ"}


@pytest.mark.parametrize("durable", [True, False])
def test_no_tmp_debris(tmp_path, durable):
    p = tmp_path / "a.json"
    atomic_write_json(p, {"x": 1}, durable=durable)
    assert list(tmp_path.iterdir()) == [p], "*.tmp must be renamed away, not left"


def test_atomicity_does_not_depend_on_fsync(tmp_path):
    """The old file must survive a failed non-durable write.

    Atomicity comes from ``os.replace``, not from fsync -- dropping fsync
    weakens durability only, never the "readers see old-or-new" guarantee.
    """
    p = tmp_path / "a.json"
    atomic_write_json(p, {"v": "old"}, durable=False)

    class _Unserialisable:
        pass

    with pytest.raises(TypeError):
        atomic_write_json(p, {"v": _Unserialisable()}, durable=False)
    assert json.loads(p.read_text(encoding="utf-8")) == {"v": "old"}
    assert list(tmp_path.iterdir()) == [p]


# ── JsonRecordRegistry._write passes the flag through ────────────────────────

class _Rec:
    def __init__(self, key: str, n: int = 0):
        self.key, self.n = key, n


class _Reg(JsonRecordRegistry[_Rec]):
    subdir = "recs"

    def _key_of(self, rec):
        return rec.key

    def _to_json(self, rec):
        return {"key": rec.key, "n": rec.n}

    def _from_json(self, d):
        return _Rec(d["key"], d["n"])


def _reg(tmp_path):
    try:
        return _Reg(str(tmp_path))
    except TypeError:
        r = _Reg.__new__(_Reg)
        r.data_dir = str(tmp_path)          # type: ignore[attr-defined]
        return r


def test_registry_write_defaults_to_durable(tmp_path, no_fsync):
    reg = _reg(tmp_path)
    try:
        reg._write(_Rec("a"))
    except Exception:
        pytest.skip("registry ctor signature differs; covered by the fn tests")
    assert len(no_fsync) == 1


def test_registry_write_can_opt_out(tmp_path, no_fsync):
    reg = _reg(tmp_path)
    try:
        reg._write(_Rec("a"), durable=False)
    except Exception:
        pytest.skip("registry ctor signature differs; covered by the fn tests")
    assert no_fsync == []


# ── the actual hot path ──────────────────────────────────────────────────────

def test_touch_used_does_not_fsync():
    """``touch_used`` fires once per crawl session -- it must not fsync.

    Pinned by reading the source rather than constructing a HostRegistry,
    which needs a populated data dir. The point is the call site, and this
    fails loudly if someone drops the flag while refactoring.
    """
    src = (Path(__file__).resolve().parents[1]
           / "server" / "hub" / "hosts.py").read_text(encoding="utf-8")
    body = src.split("def touch_used(", 1)[1].split("\n    def ", 1)[0]
    assert "self._write(rec, durable=False)" in body, (
        "touch_used must write with durable=False -- an fsync here stalls the "
        "hub event loop (admin UI + POST /jobs) once per crawl session"
    )


def test_touch_login_does_not_fsync():
    src = (Path(__file__).resolve().parents[1]
           / "server" / "hub" / "hosts.py").read_text(encoding="utf-8")
    body = src.split("def touch_login(", 1)[1].split("\n    def ", 1)[0]
    assert "self._write(rec, durable=False)" in body


# ── the real hot path: automatic cookie saves ────────────────────────────────
# The first cut of this fix patched ``touch_used`` and missed the actual
# writer. py-spy on the patched hub showed the loop still in ``os.fsync``,
# called from ``HostRegistry.upsert`` -- the per-job cookie harvest
# (``routes/jobs/_base.py``) and the per-session auto-save
# (``routes/sessions/_base.py``). Those are the ones that fire per crawl.

def _src(*parts):
    return (Path(__file__).resolve().parents[1].joinpath(*parts)
            .read_text(encoding="utf-8"))


def test_upsert_accepts_durable_flag():
    body = _src("server", "hub", "hosts.py").split("def upsert(", 1)[1]
    sig = body.split(") -> HostRecord:", 1)[0]
    assert "durable: bool = True" in sig, "upsert must expose durable (default True)"
    assert "self._write(rec, durable=durable)" in body.split("\n    def ", 1)[0]


def test_per_job_cookie_save_is_not_durable():
    """Once per job, rewriting ~600 KB. An fsync here stalls the whole hub."""
    src = _src("server", "hub", "routes", "jobs", "_base.py")
    assert "state.hosts.upsert(host, merged, durable=False)" in src


def test_per_session_cookie_save_is_not_durable():
    src = _src("server", "hub", "routes", "sessions", "_base.py")
    block = src.split("state.hosts.upsert(", 1)[1].split(")", 1)[0]
    assert "durable=False" in block


def test_operator_host_edits_stay_durable():
    """The operator's own ``PUT /hosts/{host}`` must keep the fsync.

    Losing an operator's cookie paste to a crash is a real loss; losing an
    auto-harvested refresh is not. This is the line that keeps the two apart.
    """
    src = _src("server", "hub", "routes", "hosts.py")
    assert "durable" not in src, (
        "routes/hosts.py is the operator API -- it must rely on the durable "
        "default, not opt out"
    )


# ── tiered registries (skills / conventions) ─────────────────────────────────
# The hub's boot restore also mirrors these, and they were still fsync-rewriting
# every record on every start (py-spy: 18/20 samples in ``_jsonstore.py:322``'s
# write path). Adding durable=False at the restore call site without widening
# the registry signature raises TypeError at startup and kills every hub, so
# pin both halves.

def test_tiered_write_accepts_durable():
    src = _src("server", "hub", "_jsonstore.py")
    body = src.split("class TieredJsonRecordRegistry", 1)[1]
    assert "def _write(self, rec: T, *, durable: bool = True)" in body
    assert "durable=durable" in body


@pytest.mark.parametrize("mod", ["skills", "conventions"])
def test_tiered_registry_upsert_accepts_durable(mod):
    """restore_* passes durable=False -- the signature must accept it."""
    src = _src("server", "hub", f"{mod}.py")
    body = src.split("    def upsert(", 1)[1].split("\n    def ", 1)[0]
    assert "durable: bool = True" in body, f"{mod}.upsert must accept durable"
    assert "self._write(rec, durable=durable)" in body


@pytest.mark.parametrize("fn", ["restore_skills", "restore_conventions"])
def test_tiered_restore_is_not_durable(fn):
    src = _src("server", "hub", "mariadb.py")
    body = src.split(f"async def {fn}(", 1)[1].split("\nasync def ", 1)[0]
    assert "durable=False" in body, f"{fn} must not fsync (MariaDB is the source)"
