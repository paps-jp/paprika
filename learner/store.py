"""MariaDB access for the autopilot (playbooks, key state, Solver runs).

Thin on purpose: every decision is made by pure functions elsewhere
(``playbook.decide``, ``scheduler.rank``); this file only fetches their
inputs and writes their outputs. It shares the ledger's connection pool.
All tables are prefixed ``learn_`` and owned by the learner.
"""
from __future__ import annotations

import datetime as _dt
import json
from collections import defaultdict

from learner import playbook as P
from learner.scheduler import TARGET_INTENT, TARGET_REASONS, KeyCandidate, KeyState

#: Arms whose jobs ran WITHOUT learned steps of any kind being new: the
#: population a key's "how often does plain fetching fail" is measured on.
_BASE_ARMS = ("control", "recipe", "holdout", "pb_control", "pb_holdout")


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)


def _in(n: int) -> str:
    return ", ".join(["%s"] * n)


def _loads(v, default):
    if v is None:
        return default
    if isinstance(v, (dict, list)):
        return v
    try:
        return json.loads(v)
    except Exception:
        return default


class Store:
    def __init__(self, pool) -> None:
        self._pool = pool

    async def ensure_schema(self) -> None:
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                for stmt in P.SCHEMA:
                    await cur.execute(stmt)
            await conn.commit()

    async def _fetch(self, sql: str, params: tuple = ()) -> list[tuple]:
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(sql, params)
                rows = await cur.fetchall()
            await conn.commit()  # end the read snapshot (REPEATABLE READ)
            return list(rows)

    async def _exec(self, sql: str, params: tuple = ()) -> int:
        async with self._pool.acquire() as conn:
            try:
                async with conn.cursor() as cur:
                    await cur.execute(sql, params)
                    last = cur.lastrowid
                await conn.commit()
                return int(last or 0)
            except Exception:
                await conn.rollback()
                raise

    # ---- playbooks -------------------------------------------------------

    async def load_playbooks(self, *, include_retired: bool = False) -> list[P.Playbook]:
        where = "" if include_retired else "WHERE state <> 'retired'"
        rows = await self._fetch(
            "SELECT id, host, template, intent, state, actions, version, apply_pct, holdout_pct, "
            f"promoted_diff, state_changed_at, created_at, reason, provenance FROM learn_playbooks {where}"
        )
        return [
            P.Playbook(
                id=int(r[0]), host=r[1], template=r[2], intent=r[3], state=r[4],
                actions=_loads(r[5], []), version=int(r[6]), apply_pct=int(r[7]),
                holdout_pct=int(r[8]), promoted_diff=r[9], state_changed_at=r[10],
                created_at=r[11], reason=r[12] or "", provenance=_loads(r[13], {}),
            )
            for r in rows
        ]

    async def insert_candidate(self, *, host: str, template: str, intent: str, actions: list,
                               provenance: dict, holdout_pct: int) -> int:
        now = _utcnow()
        return await self._exec(
            "INSERT INTO learn_playbooks (host, template, intent, version, state, apply_pct, "
            "holdout_pct, actions, provenance, reason, created_at, state_changed_at) "
            "VALUES (%s, %s, %s, 1, 'candidate', 0, %s, %s, %s, 'compiled', %s, %s)",
            (host, template, intent, holdout_pct,
             json.dumps(actions, ensure_ascii=False, separators=(",", ":")),
             json.dumps(provenance, ensure_ascii=False, separators=(",", ":")), now, now),
        )

    async def set_state(self, pb_id: int, state: str, reason: str, *,
                        apply_pct: int | None = None, promoted_diff: float | None = None) -> None:
        sets = ["state = %s", "reason = %s", "state_changed_at = %s"]
        params: list = [state, reason[:64], _utcnow()]
        if apply_pct is not None:
            sets.append("apply_pct = %s")
            params.append(int(apply_pct))
        if promoted_diff is not None:
            sets.append("promoted_diff = %s")
            params.append(float(promoted_diff))
        params.append(int(pb_id))
        await self._exec(f"UPDATE learn_playbooks SET {', '.join(sets)} WHERE id = %s", tuple(params))

    async def playbook_counts(self, pb_ids: list[int], *, since_days: int | None = None) -> dict:
        """{pb_id: {arm: {n_<outcome>: int}}}; ``since_days`` limits the window."""
        if not pb_ids:
            return {}
        cols = ("n", "n_success", "n_partial", "n_fail", "n_blocked", "n_dead",
                "n_infra", "n_na", "n_unknown")
        sql = (
            "SELECT pb_id, arm, " + ", ".join(f"SUM({c})" for c in cols)
            + f" FROM learn_playbook_stats WHERE pb_id IN ({_in(len(pb_ids))})"
        )
        params: list = list(pb_ids)
        if since_days is not None:
            sql += " AND day >= UTC_DATE() - INTERVAL %s DAY"
            params.append(max(int(since_days) - 1, 0))
        sql += " GROUP BY pb_id, arm"
        out: dict = defaultdict(dict)
        for r in await self._fetch(sql, tuple(params)):
            out[int(r[0])][r[1]] = {c: int(v or 0) for c, v in zip(cols, r[2:])}
        return out

    async def last_applied_days(self, pb_ids: list[int]) -> dict:
        """{pb_id: days since a job last ran the playbook}."""
        if not pb_ids:
            return {}
        rows = await self._fetch(
            "SELECT pb_id, DATEDIFF(UTC_DATE(), MAX(day)) FROM learn_playbook_stats "
            f"WHERE pb_id IN ({_in(len(pb_ids))}) AND arm IN ('trial', 'active') AND n > 0 "
            "GROUP BY pb_id", tuple(pb_ids),
        )
        return {int(r[0]): float(r[1]) for r in rows if r[1] is not None}

    async def key_base_counts(self, host: str, template: str, intent: str, *, days: int = 7) -> dict:
        rows = await self._fetch(
            "SELECT SUM(n_success), SUM(n_partial), SUM(n_fail), SUM(n_blocked) "
            "FROM learn_template_stats_daily WHERE host = %s AND template = %s AND intent = %s "
            f"AND day >= UTC_DATE() - INTERVAL %s DAY AND arm IN ({_in(len(_BASE_ARMS))})",
            (host, template, intent, max(days - 1, 0), *_BASE_ARMS),
        )
        r = rows[0] if rows else (0, 0, 0, 0)
        return {"n_success": int(r[0] or 0), "n_partial": int(r[1] or 0),
                "n_fail": int(r[2] or 0), "n_blocked": int(r[3] or 0)}

    # ---- scheduler inputs ------------------------------------------------

    async def candidates(self, *, hours: int = 48, limit: int = 150,
                         reasons: tuple = TARGET_REASONS) -> list[KeyCandidate]:
        """Keys with recent failures of the target class, with their 7-day
        counts and a few of the failing URLs."""
        reasons = tuple(reasons)
        if not reasons:
            return []
        rows = await self._fetch(
            "SELECT host, template, reason, COUNT(*), COUNT(DISTINCT url) "
            "FROM learn_outcome_events WHERE intent = %s AND outcome = 'fail' "
            f"AND reason IN ({_in(len(reasons))}) AND arm IN ({_in(len(_BASE_ARMS))}) "
            "AND ts > UTC_TIMESTAMP() - INTERVAL %s HOUR GROUP BY host, template, reason",
            (TARGET_INTENT, *reasons, *_BASE_ARMS, int(hours)),
        )
        per_key: dict = defaultdict(lambda: {"reasons": defaultdict(int), "urls": 0})
        for host, template, reason, n, urls in rows:
            d = per_key[(host, template)]
            d["reasons"][reason] += int(n)
            d["urls"] = max(d["urls"], int(urls))
        keys = sorted(
            (k for k, d in per_key.items() if d["urls"] >= 3),
            key=lambda k: -sum(per_key[k]["reasons"].values()),
        )[:limit]
        out: list[KeyCandidate] = []
        for host, template in keys:
            d = per_key[(host, template)]
            klass = max(d["reasons"].items(), key=lambda kv: kv[1])[0]
            base = await self.key_base_counts(host, template, TARGET_INTENT)
            urls = await self._fetch(
                "SELECT url FROM learn_outcome_events WHERE host = %s AND template = %s "
                "AND intent = %s AND outcome = 'fail' "
                f"AND reason IN ({_in(len(reasons))}) AND arm IN ({_in(len(_BASE_ARMS))}) "
                "AND ts > UTC_TIMESTAMP() - INTERVAL %s HOUR ORDER BY ts DESC LIMIT 12",
                (host, template, TARGET_INTENT, *reasons, *_BASE_ARMS, int(hours)),
            )
            out.append(KeyCandidate(
                host=host, template=template, intent=TARGET_INTENT, klass=klass,
                fails_7d=base["n_fail"],
                scored_7d=sum(base.values()), success_7d=base["n_success"],
                urls=[u[0] for u in urls if u[0]],
            ))
        return out

    async def key_states(self) -> dict:
        rows = await self._fetch(
            "SELECT host, template, intent, runs, solved, fail_streak, dormant_until, last_run_at "
            "FROM learn_key_state"
        )
        return {
            (r[0], r[1], r[2]): KeyState(runs=int(r[3]), solved=int(r[4]), fail_streak=int(r[5]),
                                         dormant_until=r[6], last_run_at=r[7])
            for r in rows
        }

    async def class_stats(self) -> dict:
        rows = await self._fetch(
            "SELECT klass, SUM(solved), SUM(runs) FROM learn_key_state GROUP BY klass"
        )
        return {r[0]: (int(r[1] or 0), int(r[2] or 0)) for r in rows}

    async def usage_today(self) -> tuple[int, int]:
        """(Solver runs, model tokens) spent this UTC day. Infra results
        consumed no decision and are not counted as runs."""
        rows = await self._fetch(
            "SELECT SUM(result <> 'infra'), SUM(tokens) FROM learn_solver_runs "
            "WHERE ts >= UTC_DATE()"
        )
        r = rows[0] if rows else (0, 0)
        return int(r[0] or 0), int(r[1] or 0)

    # ---- status (read-only; feeds learner/status.py) ---------------------

    async def solver_days(self, *, days: int = 7) -> list[tuple]:
        """(day, result, runs, tokens) per UTC day."""
        return await self._fetch(
            "SELECT DATE(ts), result, COUNT(*), SUM(tokens) FROM learn_solver_runs "
            "WHERE ts >= UTC_DATE() - INTERVAL %s DAY GROUP BY DATE(ts), result",
            (max(int(days) - 1, 0),),
        )

    async def recent_runs(self, *, limit: int = 30) -> list[tuple]:
        return await self._fetch(
            "SELECT ts, host, template, klass, url, result, reason, steps, tokens, duration_s, pb_id "
            "FROM learn_solver_runs ORDER BY id DESC LIMIT %s", (int(limit),),
        )

    async def key_summary(self) -> list[tuple]:
        """(klass, keys, runs, solved, keys dormant right now)."""
        return await self._fetch(
            "SELECT klass, COUNT(*), SUM(runs), SUM(solved), "
            "SUM(dormant_until IS NOT NULL AND dormant_until > UTC_TIMESTAMP()) "
            "FROM learn_key_state GROUP BY klass"
        )

    async def outcome_days(self, *, days: int = 7) -> list[tuple]:
        """(day, intent, n, one sum per verify.OUTCOMES) over every arm."""
        from learner.verify import OUTCOMES
        sums = ", ".join(f"SUM(n_{o})" for o in OUTCOMES)
        return await self._fetch(
            f"SELECT day, intent, SUM(n), {sums} FROM learn_template_stats_daily "
            "WHERE day >= UTC_DATE() - INTERVAL %s DAY GROUP BY day, intent",
            (max(int(days) - 1, 0),),
        )

    async def fail_reasons(self, *, hours: int = 24, limit: int = 12) -> list[tuple]:
        """(intent, reason, n) of jobs judged ``fail`` in the last hours.
        Counted on the sampled detail events, so the numbers are a sample."""
        return await self._fetch(
            "SELECT intent, reason, COUNT(*) FROM learn_outcome_events WHERE outcome = 'fail' "
            "AND ts > UTC_TIMESTAMP() - INTERVAL %s HOUR GROUP BY intent, reason "
            "ORDER BY COUNT(*) DESC LIMIT %s", (int(hours), int(limit)),
        )

    # ---- Solver outputs --------------------------------------------------

    async def record_run(self, *, host: str, template: str, intent: str, klass: str, url: str,
                         result: str, reason: str, steps: int, llm_calls: int, tokens: int,
                         duration_s: float, pb_id: int | None, manifest: dict, trace: list) -> int:
        return await self._exec(
            "INSERT INTO learn_solver_runs (ts, host, template, intent, klass, url, result, reason, "
            "steps, llm_calls, tokens, duration_s, pb_id, manifest, trace) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (_utcnow(), host, template, intent, klass, url[:600], result[:32], reason[:120],
             int(steps), int(llm_calls), int(tokens), float(duration_s), pb_id,
             json.dumps(manifest, ensure_ascii=False, separators=(",", ":")),
             json.dumps(trace, ensure_ascii=False, separators=(",", ":"), default=str)[:60000]),
        )

    async def update_key_state(self, *, host: str, template: str, intent: str, klass: str,
                               solved: bool, result: str,
                               dormant_until: _dt.datetime | None) -> None:
        """Count one finished (non-infra) run against the key."""
        now = _utcnow()
        await self._exec(
            "INSERT INTO learn_key_state (host, template, intent, klass, runs, solved, fail_streak, "
            "dormant_until, last_run_at, last_result) VALUES (%s, %s, %s, %s, 1, %s, %s, %s, %s, %s) "
            "ON DUPLICATE KEY UPDATE klass = VALUES(klass), runs = runs + 1, "
            "solved = solved + VALUES(solved), "
            "fail_streak = IF(VALUES(solved) = 1, 0, fail_streak + 1), "
            "dormant_until = VALUES(dormant_until), last_run_at = VALUES(last_run_at), "
            "last_result = VALUES(last_result)",
            (host, template, intent, klass, 1 if solved else 0, 0 if solved else 1,
             dormant_until, now, result[:32]),
        )

    async def fail_streak(self, host: str, template: str, intent: str) -> int:
        rows = await self._fetch(
            "SELECT fail_streak FROM learn_key_state WHERE host = %s AND template = %s AND intent = %s",
            (host, template, intent),
        )
        return int(rows[0][0]) if rows else 0

    async def outcomes(self, job_ids: list[str]) -> dict:
        """{job_id: (outcome, reason)} for the jobs the ledger has judged."""
        if not job_ids:
            return {}
        rows = await self._fetch(
            f"SELECT job_id, outcome, reason FROM learn_outcome_events WHERE job_id IN ({_in(len(job_ids))})",
            tuple(job_ids),
        )
        return {r[0]: (r[1], r[2]) for r in rows}
