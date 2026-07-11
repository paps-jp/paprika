"""Regression: the salvage collapse-guard (_alive_collapsed) must treat a
collapsed cross-hub alive set as a degraded view, NOT a real mass-ghost.

2026-07-09 incident: when hubs held a stale Redis client, stats_async read
alive=0 while the durable ledger still held 167 workers -- so salvage
SSH-restarted the ENTIRE fleet in a churn loop. The guard skips a salvage pass
when the alive set has collapsed to empty, or to below a small fraction of the
ledger. These tests pin the exact truth table.

See memory: stale-redis-hub-dereg-salvage-storm.
"""

from server.hub._salvage import _alive_collapsed

_RATIO = 0.2  # PAPRIKA_SALVAGE_MIN_ALIVE_RATIO default


def test_empty_alive_with_populated_ledger_is_collapsed():
    # The exact incident signature: alive=0, ledger=167.
    assert _alive_collapsed(0, 167, _RATIO) is True


def test_full_alive_is_not_collapsed():
    assert _alive_collapsed(167, 167, _RATIO) is False


def test_tiny_fraction_is_collapsed():
    # 10/167 = 0.06 < 0.2 -> degraded view, skip salvage.
    assert _alive_collapsed(10, 167, _RATIO) is True


def test_healthy_fraction_above_ratio_is_not_collapsed():
    # 40/167 = 0.24 >= 0.2 -> plausibly real; salvage may proceed.
    assert _alive_collapsed(40, 167, _RATIO) is False


def test_empty_ledger_disables_guard():
    # Nothing to compare against -> not collapsed (guard off), so a genuinely
    # empty fleet on first boot doesn't wedge salvage.
    assert _alive_collapsed(0, 0, _RATIO) is False


def test_ratio_boundary_is_exclusive():
    # ratio check is strict '<': exactly at the threshold is NOT collapsed.
    assert _alive_collapsed(2, 10, 0.2) is False  # 0.20, not < 0.20
    assert _alive_collapsed(1, 10, 0.2) is True   # 0.10 < 0.20


def test_ratio_zero_leaves_only_empty_guard():
    # PAPRIKA_SALVAGE_MIN_ALIVE_RATIO=0 disables the fraction check; only a
    # fully-empty alive set counts as collapsed.
    assert _alive_collapsed(1, 167, 0.0) is False
    assert _alive_collapsed(0, 167, 0.0) is True
