"""Regression: yt-dlp ETA parsing that feeds the slow-download early-abort
gate (#3, 2026-07-10).

That night ~200 large-but-moderate-speed downloads (141 KiB/s, ETA ~4h) piled
up because they never tripped the raw min-rate floor; the shared Proxmox nodes
overloaded until workers heartbeat-starved into hub-side ghosts and the fleet
drained 105->78. The fix aborts a download once yt-dlp's own smoothed ETA
stays above PAPRIKA_YTDLP_MAX_ETA_S (default 1500s = 25min).

_eta_line_to_s is the pure parser the gate's decision rests on: a wrong parse
means the gate never fires (cascade returns) or fires on healthy downloads.
These tests pin the MM:SS / HH:MM:SS grammar and the default cap comparison.

See memory: worker-ghost-ytdlp-runaway.
"""

from server.worker.agent.video import _eta_line_to_s, _parse_dl_progress

# Default gate cap (PAPRIKA_YTDLP_MAX_ETA_S); the incident downloads sat far
# above this.
_DEFAULT_MAX_ETA_S = 1500


def test_mm_ss_eta():
    assert _eta_line_to_s("[download]  45.2% of 1.20GiB at 5.00MiB/s ETA 00:30") == 30
    assert _eta_line_to_s("[download]  1.0% ETA 40:00") == 40 * 60


def test_hh_mm_ss_eta():
    # The exact case the min-rate floor missed: a multi-hour ETA.
    assert _eta_line_to_s("[download] 0.5% ETA 4:00:00") == 4 * 3600
    assert _eta_line_to_s("[download] 0.5% ETA 1:02:03") == 3600 + 2 * 60 + 3


def test_no_eta_returns_none():
    assert _eta_line_to_s("[download]  0.0% of ~1.00GiB at Unknown B/s") is None
    assert _eta_line_to_s("some unrelated log line") is None


def test_multi_hour_eta_exceeds_default_cap():
    # The 2026-07-10 signature (ETA ~4h) must read as "over the cap".
    eta = _eta_line_to_s("[download] 2.0% of 2.00GiB at 141.00KiB/s ETA 4:00:00")
    assert eta is not None and eta > _DEFAULT_MAX_ETA_S


def test_short_eta_stays_under_default_cap():
    # A healthy download (ETA 20:00 = 1200s) must NOT trip the 1500s gate.
    eta = _eta_line_to_s("[download] 50.0% of 500MiB at 5.00MiB/s ETA 20:00")
    assert eta is not None and eta <= _DEFAULT_MAX_ETA_S


def test_parse_dl_progress_extracts_eta_field():
    prog = _parse_dl_progress(
        "[download]  45.2% of 1.20GiB at 5.00MiB/s ETA 00:30"
    )
    assert prog is not None
    assert prog.get("state") == "downloading"
    assert prog.get("eta") == "00:30"
