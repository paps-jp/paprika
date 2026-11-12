"""The outcome ledger (design §6): the single source of every counter.

Two tables in MariaDB, both owned by the learner (prefix ``learn_``):

``learn_template_stats_daily``
    Exact per-day counts for EVERY job, keyed by
    (host, template, intent, day, verifier_version, arm). Small: a few rows
    per active template per day. ``arm`` is the experiment group the job ran
    in, so "does this recipe help" is a comparison of two rows of the same
    key over the same days. Everything a scheduler needs -- traffic,
    verified success rate, how blocked a template is -- is a SUM over it.

``learn_outcome_events``
    Per-job detail for audits and debugging -- NOT for counting (the daily
    table is exact; this one is a sample). Kept: every solver job, every
    scored holdout job, 10% of scored non-successes, 1% of everything else.
    Storing all ~770k jobs/day would just rebuild the jobs table; on
    2026-11-12 non-successes alone ran at ~180k/day.

The aggregation is plain Python (``DailyAggregator``) so it can be tested
without a database; ``MariaLedger`` only knows how to write rows.
"""
from __future__ import annotations

import datetime as _dt
import json
import zlib
from collections import defaultdict
from dataclasses import dataclass

from learner.verify import OUTCOMES, SCORED, SUCCESS, Verdict

try:  # single source of truth for the template key (pure stdlib module)
    from server.hub._page_role import templatize as _templatize
except Exception:  # pragma: no cover - learner deployed without the hub tree
    _templatize = None

#: Detail-row sampling: hash(job_id) % 100 < pct.
SAMPLE_PCT = 1               # ordinary jobs
SAMPLE_PCT_NONSUCCESS = 10   # scored non-successes (fail / partial / blocked)

MAX_HOST = 190
MAX_TEMPLATE = 300

SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS learn_template_stats_daily (
        host VARCHAR(190) NOT NULL,
        template VARCHAR(300) NOT NULL,
        intent VARCHAR(16) NOT NULL,
        day DATE NOT NULL,
        verifier_version INT NOT NULL,
        n INT NOT NULL DEFAULT 0,
        n_success INT NOT NULL DEFAULT 0,
        n_partial INT NOT NULL DEFAULT 0,
        n_fail INT NOT NULL DEFAULT 0,
        n_blocked INT NOT NULL DEFAULT 0,
        n_dead INT NOT NULL DEFAULT 0,
        n_infra INT NOT NULL DEFAULT 0,
        n_na INT NOT NULL DEFAULT 0,
        n_unknown INT NOT NULL DEFAULT 0,
        n_recipe INT NOT NULL DEFAULT 0,
        n_recipe_success INT NOT NULL DEFAULT 0,
        updated_at DATETIME NOT NULL,
        arm VARCHAR(16) NOT NULL DEFAULT 'mixed',
        PRIMARY KEY (host, template, intent, day, verifier_version, arm),
        KEY idx_day (day)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin
    """,
    """
    CREATE TABLE IF NOT EXISTS learn_outcome_events (
        job_id VARCHAR(64) NOT NULL,
        ts DATETIME NOT NULL,
        host VARCHAR(190) NOT NULL,
        template VARCHAR(300) NOT NULL,
        intent VARCHAR(16) NOT NULL,
        arm VARCHAR(16) NOT NULL,
        outcome VARCHAR(16) NOT NULL,
        reason VARCHAR(48) NOT NULL,
        verifier_version INT NOT NULL,
        verified_by VARCHAR(8) NOT NULL,
        recipe_pattern VARCHAR(300) NOT NULL DEFAULT '',
        url VARCHAR(600) NOT NULL DEFAULT '',
        evidence JSON NULL,
        PRIMARY KEY (job_id),
        KEY idx_key_ts (host, template, intent, ts),
        KEY idx_ts (ts),
        KEY idx_outcome_ts (outcome, ts)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin
    """,
    # Per-playbook, per-arm, per-day counts: what the trial -> active ->
    # retired decisions are made from (learner/playbook.py). Daily so a
    # "recent window" is a SUM over a few rows.
    """
    CREATE TABLE IF NOT EXISTS learn_playbook_stats (
        pb_id BIGINT NOT NULL,
        arm VARCHAR(16) NOT NULL,
        day DATE NOT NULL,
        n INT NOT NULL DEFAULT 0,
        n_success INT NOT NULL DEFAULT 0,
        n_partial INT NOT NULL DEFAULT 0,
        n_fail INT NOT NULL DEFAULT 0,
        n_blocked INT NOT NULL DEFAULT 0,
        n_dead INT NOT NULL DEFAULT 0,
        n_infra INT NOT NULL DEFAULT 0,
        n_na INT NOT NULL DEFAULT 0,
        n_unknown INT NOT NULL DEFAULT 0,
        updated_at DATETIME NOT NULL,
        PRIMARY KEY (pb_id, arm, day)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin
    """,
)


def template_of(url: str) -> str:
    if _templatize is None:
        return "/"
    try:
        return (_templatize(url) or "/")[:MAX_TEMPLATE]
    except Exception:
        return "/"


def day_of(ts: float | None) -> str:
    """UTC day (the hub clock and the jobs table are UTC)."""
    t = _dt.datetime.fromtimestamp(float(ts or 0.0), _dt.timezone.utc)
    return t.strftime("%Y-%m-%d")


def in_sample(job_id: str, pct: int = SAMPLE_PCT) -> bool:
    """Deterministic: the same job is in or out no matter who asks."""
    return (zlib.crc32((job_id or "").encode("utf-8")) % 100) < pct


#: Playbook experiment arms, stamped by server/hub/_playbooks.py.
PB_ARMS = ("trial", "pb_control", "active", "pb_holdout")
ARMS = ("control", "recipe", "holdout", "solver") + PB_ARMS


def arm_of(ev: dict) -> str:
    """Which experimental group the job ran in.

    control     no recipe matched this URL
    recipe      a legacy host recipe was replayed
    holdout     a legacy recipe matched but was deliberately withheld
                (server/hub/_learn_arm.py) -- the comparison group for ``recipe``
    solver      a job the learner itself submitted (replay / generalisation
                test), or a legacy escalated job
    trial       a trial playbook was replayed
    pb_control  a trial playbook matched and was withheld
    active      an active playbook was replayed
    pb_holdout  an active playbook matched and was withheld
    """
    arm = str(ev.get("arm") or "")
    if ev.get("escalated_from") or arm == "solver":
        return "solver"
    if arm in PB_ARMS and ev.get("pb") is not None:
        return arm
    if arm == "holdout" and not ev.get("recipe_applied"):
        return "holdout"
    return "recipe" if ev.get("recipe_applied") else "control"


def pb_credits(ev: dict, arm: str) -> list[tuple[int, str]]:
    """Which (playbook, arm) counters one job feeds.

    A job belongs to the playbook it was assigned to. An ``active`` job on
    a key that also has a trial is, in addition, that trial's comparison
    group (the event names it in ``pb_vs``).
    """
    if arm not in PB_ARMS:
        return []
    try:
        out = [(int(ev["pb"]), arm)]
        if arm == "active" and ev.get("pb_vs") is not None:
            out.append((int(ev["pb_vs"]), "pb_control"))
        return out
    except (KeyError, TypeError, ValueError):
        return []


def keep_detail(ev: dict, verdict: Verdict, arm: str) -> bool:
    job_id = str(ev.get("job_id") or "")
    if arm == "solver":
        return True
    if arm in PB_ARMS:
        # Every job of a playbook experiment: the lifecycle decisions made
        # from them must be auditable job by job.
        return True
    if arm == "holdout" and verdict.scored:
        return True
    if verdict.scored and verdict.outcome != SUCCESS:
        return in_sample(job_id, SAMPLE_PCT_NONSUCCESS)
    return in_sample(job_id)


@dataclass(frozen=True)
class Key:
    host: str
    template: str
    intent: str
    day: str
    version: int
    arm: str = "control"


class DailyAggregator:
    """In-memory counters between flushes. ``add`` is O(1); ``drain`` hands
    back the deltas and resets, so a flush is an additive UPSERT."""

    def __init__(self) -> None:
        self._c: dict[Key, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        self._pb: dict[tuple[int, str, str], dict[str, int]] = defaultdict(lambda: defaultdict(int))
        self._detail: list[dict] = []
        self.seen = 0

    def add(self, ev: dict, verdict: Verdict) -> None:
        host = str(ev.get("host") or "")[:MAX_HOST]
        if not host:
            return
        arm = arm_of(ev)
        key = Key(host, template_of(str(ev.get("url") or "")), verdict.intent,
                  day_of(ev.get("ts")), verdict.version, arm)
        c = self._c[key]
        c["n"] += 1
        c["n_" + verdict.outcome] += 1
        if arm == "recipe":
            c["n_recipe"] += 1
            if verdict.outcome == SUCCESS:
                c["n_recipe_success"] += 1
        for pb_id, pb_arm in pb_credits(ev, arm):
            p = self._pb[(pb_id, pb_arm, key.day)]
            p["n"] += 1
            p["n_" + verdict.outcome] += 1
        self.seen += 1
        if keep_detail(ev, verdict, arm):
            self._detail.append({
                "job_id": str(ev.get("job_id") or "")[:64],
                "ts": float(ev.get("ts") or 0.0),
                "host": key.host, "template": key.template, "intent": key.intent,
                "arm": arm, "outcome": verdict.outcome, "reason": verdict.reason[:48],
                "verifier_version": verdict.version, "verified_by": verdict.verified_by,
                "recipe_pattern": str(ev.get("recipe_pattern") or "")[:300],
                "url": str(ev.get("url") or "")[:600],
                "evidence": verdict.evidence or {},
            })

    def pending(self) -> int:
        return len(self._c) + len(self._detail) + len(self._pb)

    def drain_playbooks(self) -> list[tuple[tuple[int, str, str], dict]]:
        """Per-playbook deltas: [((pb_id, arm, day), counts)]. Drained by
        the same flush as ``drain`` so both land in one transaction."""
        out = [(k, dict(v)) for k, v in self._pb.items()]
        self._pb = defaultdict(lambda: defaultdict(int))
        return out

    def drain(self) -> tuple[list[tuple[Key, dict]], list[dict]]:
        daily = [(k, dict(v)) for k, v in self._c.items()]
        detail = self._detail
        self._c = defaultdict(lambda: defaultdict(int))
        self._detail = []
        return daily, detail


_COUNT_COLS = ["n"] + ["n_" + o for o in OUTCOMES] + ["n_recipe", "n_recipe_success"]
_PB_COUNT_COLS = ["n"] + ["n_" + o for o in OUTCOMES]


def pb_upsert_sql() -> str:
    cols = ["pb_id", "arm", "day"] + _PB_COUNT_COLS + ["updated_at"]
    ph = ", ".join(["%s"] * len(cols))
    upd = ", ".join(f"{c} = {c} + VALUES({c})" for c in _PB_COUNT_COLS)
    return (
        f"INSERT INTO learn_playbook_stats ({', '.join(cols)}) VALUES ({ph}) "
        f"ON DUPLICATE KEY UPDATE {upd}, updated_at = VALUES(updated_at)"
    )


def pb_params(key: tuple[int, str, str], counts: dict, now: _dt.datetime) -> tuple:
    pb_id, arm, day = key
    return (pb_id, arm, day, *[int(counts.get(c, 0)) for c in _PB_COUNT_COLS], now)


def daily_upsert_sql() -> str:
    cols = ["host", "template", "intent", "day", "verifier_version", "arm"] + _COUNT_COLS + ["updated_at"]
    ph = ", ".join(["%s"] * len(cols))
    upd = ", ".join(f"{c} = {c} + VALUES({c})" for c in _COUNT_COLS)
    return (
        f"INSERT INTO learn_template_stats_daily ({', '.join(cols)}) VALUES ({ph}) "
        f"ON DUPLICATE KEY UPDATE {upd}, updated_at = VALUES(updated_at)"
    )


def daily_params(key: Key, counts: dict, now: _dt.datetime) -> tuple:
    return (
        key.host, key.template, key.intent, key.day, key.version, key.arm,
        *[int(counts.get(c, 0)) for c in _COUNT_COLS], now,
    )


#: A job can be reported twice (fetch result, then the deferred download's
#: final result). The later event carries more facts, so it wins.
DETAIL_UPSERT_SQL = (
    "INSERT INTO learn_outcome_events (job_id, ts, host, template, intent, arm, outcome, "
    "reason, verifier_version, verified_by, recipe_pattern, url, evidence) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
    "ON DUPLICATE KEY UPDATE ts = VALUES(ts), arm = VALUES(arm), outcome = VALUES(outcome), "
    "reason = VALUES(reason), verifier_version = VALUES(verifier_version), "
    "verified_by = VALUES(verified_by), evidence = VALUES(evidence)"
)


def detail_params(row: dict) -> tuple:
    ts = _dt.datetime.fromtimestamp(row["ts"], _dt.timezone.utc).replace(tzinfo=None)
    return (
        row["job_id"], ts, row["host"], row["template"], row["intent"], row["arm"],
        row["outcome"], row["reason"], row["verifier_version"], row["verified_by"],
        row["recipe_pattern"], row["url"],
        json.dumps(row["evidence"], ensure_ascii=False, separators=(",", ":")),
    )


class MariaLedger:
    """Thin aiomysql writer. One pool; every write is a single statement
    batch in one transaction."""

    def __init__(self, pool) -> None:
        self._pool = pool

    @classmethod
    async def connect(cls, *, host: str, port: int, user: str, password: str, db: str):
        import aiomysql  # imported lazily: tests run without the driver

        pool = await aiomysql.create_pool(
            host=host, port=port, user=user, password=password, db=db,
            minsize=1, maxsize=5, autocommit=False, charset="utf8mb4",
            connect_timeout=10,
        )
        return cls(pool)

    async def ensure_schema(self) -> None:
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                for stmt in SCHEMA:
                    await cur.execute(stmt)
                # v1 tables (2026-11-12, first hours of G1) had no ``arm`` in
                # the key. Add it in place: existing rows keep 'mixed' (the
                # pre-experiment era) and are simply not part of any arm
                # comparison. Idempotent.
                await cur.execute(
                    "SELECT COUNT(*) FROM information_schema.columns "
                    "WHERE table_schema = DATABASE() "
                    "AND table_name = 'learn_template_stats_daily' AND column_name = 'arm'"
                )
                (has_arm,) = await cur.fetchone()
                if not has_arm:
                    await cur.execute(
                        "ALTER TABLE learn_template_stats_daily "
                        "ADD COLUMN arm VARCHAR(16) NOT NULL DEFAULT 'mixed', "
                        "DROP PRIMARY KEY, "
                        "ADD PRIMARY KEY (host, template, intent, day, verifier_version, arm)"
                    )
            await conn.commit()

    async def write(self, daily: list[tuple[Key, dict]], detail: list[dict],
                    playbooks: list | None = None) -> None:
        if not daily and not detail and not playbooks:
            return
        now = _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)
        async with self._pool.acquire() as conn:
            try:
                async with conn.cursor() as cur:
                    if daily:
                        await cur.executemany(
                            daily_upsert_sql(),
                            [daily_params(k, c, now) for k, c in daily],
                        )
                    if detail:
                        await cur.executemany(
                            DETAIL_UPSERT_SQL, [detail_params(r) for r in detail],
                        )
                    if playbooks:
                        await cur.executemany(
                            pb_upsert_sql(), [pb_params(k, c, now) for k, c in playbooks],
                        )
                await conn.commit()
            except Exception:
                await conn.rollback()
                raise

    async def prune(self, *, daily_days: int = 90, detail_days: int = 30) -> None:
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "DELETE FROM learn_template_stats_daily WHERE day < CURDATE() - INTERVAL %s DAY",
                    (daily_days,),
                )
                await cur.execute(
                    "DELETE FROM learn_outcome_events WHERE ts < UTC_TIMESTAMP() - INTERVAL %s DAY LIMIT 50000",
                    (detail_days,),
                )
                await cur.execute(
                    "DELETE FROM learn_playbook_stats WHERE day < CURDATE() - INTERVAL %s DAY",
                    (daily_days,),
                )
            await conn.commit()

    async def close(self) -> None:
        self._pool.close()
        await self._pool.wait_closed()


def vsr(counts: dict) -> float | None:
    """Verified success rate over the SCORED outcomes; None if nothing scored."""
    denom = sum(int(counts.get("n_" + o, 0)) for o in SCORED)
    if denom <= 0:
        return None
    return int(counts.get("n_" + SUCCESS, 0)) / denom
