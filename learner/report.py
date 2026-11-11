"""Read-side of the ledger: verified success rates and arm comparisons.

    python -m learner.report vsr      [--days 1]
    python -m learner.report recipes  [--days 7] [--min-n 20] [--top 30]

``recipes`` answers the G2 question (design §15.4): for each host whose
jobs matched a recipe, did the ``recipe`` arm do better than the
``holdout`` arm (same host, same period, recipe withheld)?

The verdict per host uses Wilson score intervals on the two success rates,
which stay honest at the small n a single host produces in a day:

    helps    recipe's LOWER bound  >  holdout's UPPER bound
    hurts    recipe's UPPER bound  <  holdout's LOWER bound
    no_diff  both arms have >= min_n scored jobs and the intervals overlap
    thin     not enough scored jobs in one of the arms yet

The math is pure functions (tested without a database); only ``main``
touches MariaDB.
"""
from __future__ import annotations

import argparse
import asyncio
import math
import os
from collections import defaultdict

from learner.verify import SCORED, SUCCESS

Z95 = 1.959963984540054


def wilson(successes: int, n: int, z: float = Z95) -> tuple[float, float]:
    """Wilson score interval for a binomial proportion. (0, 1) when n == 0."""
    if n <= 0:
        return (0.0, 1.0)
    p = successes / n
    z2 = z * z
    denom = 1.0 + z2 / n
    centre = (p + z2 / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def scored_n(counts: dict) -> int:
    return sum(int(counts.get("n_" + o, 0) or 0) for o in SCORED)


def compare_arms(recipe: dict, holdout: dict, *, min_n: int = 20) -> dict:
    """Compare two count dicts (keys ``n_<outcome>``). Pure."""
    rn, hn = scored_n(recipe), scored_n(holdout)
    rs = int(recipe.get("n_" + SUCCESS, 0) or 0)
    hs = int(holdout.get("n_" + SUCCESS, 0) or 0)
    rlo, rhi = wilson(rs, rn)
    hlo, hhi = wilson(hs, hn)
    if rn < min_n or hn < min_n:
        verdict = "thin"
    elif rlo > hhi:
        verdict = "helps"
    elif rhi < hlo:
        verdict = "hurts"
    else:
        verdict = "no_diff"
    return {
        "verdict": verdict,
        "recipe_n": rn, "recipe_rate": (rs / rn) if rn else None, "recipe_ci": (rlo, rhi),
        "holdout_n": hn, "holdout_rate": (hs / hn) if hn else None, "holdout_ci": (hlo, hhi),
        "diff": ((rs / rn) - (hs / hn)) if rn and hn else None,
    }


def group_arms(rows, *, by=("host", "intent")) -> dict:
    """rows: dicts with host/template/intent/arm + n_* -> {key: {arm: counts}}."""
    out: dict = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
    for r in rows:
        key = tuple(r[k] for k in by)
        dst = out[key][r["arm"]]
        for k, v in r.items():
            if k.startswith("n"):
                dst[k] += int(v or 0)
    return out


def recipe_report(rows, *, min_n: int = 20, by=("host", "intent")) -> list[dict]:
    res = []
    for key, arms in group_arms(rows, by=by).items():
        if "recipe" not in arms and "holdout" not in arms:
            continue
        cmp_ = compare_arms(arms.get("recipe", {}), arms.get("holdout", {}), min_n=min_n)
        cmp_["key"] = dict(zip(by, key))
        res.append(cmp_)
    res.sort(key=lambda r: -(r["recipe_n"] + r["holdout_n"]))
    return res


_COLS = ("n", "n_success", "n_partial", "n_fail", "n_blocked", "n_dead",
         "n_infra", "n_na", "n_unknown")


async def _fetch(days: int) -> list[dict]:
    import aiomysql

    conn = await aiomysql.connect(
        host=os.environ["LEARNER_MARIADB_HOST"],
        port=int(os.environ.get("LEARNER_MARIADB_PORT") or 3306),
        user=os.environ["LEARNER_MARIADB_USER"],
        password=os.environ["LEARNER_MARIADB_PASSWORD"],
        db=os.environ["LEARNER_MARIADB_DATABASE"], connect_timeout=10,
    )
    try:
        cur = await conn.cursor(aiomysql.DictCursor)
        await cur.execute(
            "SELECT host, template, intent, arm, "
            + ", ".join(f"SUM({c}) AS {c}" for c in _COLS)
            + " FROM learn_template_stats_daily "
              "WHERE day >= UTC_DATE() - INTERVAL %s DAY AND arm <> 'mixed' "
              "GROUP BY host, template, intent, arm",
            (max(days - 1, 0),),
        )
        return list(await cur.fetchall())
    finally:
        conn.close()


def _pct(x) -> str:
    return "  -  " if x is None else f"{100 * x:5.1f}"


def _print_vsr(rows) -> None:
    tot: dict = defaultdict(lambda: defaultdict(int))
    for r in rows:
        for c in _COLS:
            tot[(r["intent"], r["arm"])][c] += int(r[c] or 0)
    print(f"{'intent':6} {'arm':8} {'jobs':>8} {'scored':>8} {'VSR%':>6}  "
          f"{'succ':>7} {'part':>6} {'fail':>7} {'block':>6} {'na':>7} {'unk':>5}")
    for (intent, arm), c in sorted(tot.items()):
        sn = scored_n(c)
        print(f"{intent:6} {arm:8} {c['n']:8d} {sn:8d} {_pct(c['n_success'] / sn if sn else None):>6}  "
              f"{c['n_success']:7d} {c['n_partial']:6d} {c['n_fail']:7d} {c['n_blocked']:6d} "
              f"{c['n_na']:7d} {c['n_unknown']:5d}")


def _print_recipes(rows, min_n: int, top: int) -> None:
    rep = recipe_report(rows, min_n=min_n)
    summary: dict = defaultdict(int)
    for r in rep:
        summary[r["verdict"]] += 1
    print("hosts by verdict:", dict(summary))
    print(f"{'verdict':8} {'intent':6} {'recipe n':>8} {'rate%':>6} {'holdout n':>9} {'rate%':>6} {'diff':>6}  host")
    for r in rep[:top]:
        d = "  -  " if r["diff"] is None else f"{100 * r['diff']:+5.1f}"
        print(f"{r['verdict']:8} {r['key']['intent']:6} {r['recipe_n']:8d} {_pct(r['recipe_rate']):>6} "
              f"{r['holdout_n']:9d} {_pct(r['holdout_rate']):>6} {d:>6}  {r['key']['host']}")


def main() -> None:
    ap = argparse.ArgumentParser(prog="python -m learner.report")
    ap.add_argument("what", choices=("vsr", "recipes"))
    ap.add_argument("--days", type=int, default=1)
    ap.add_argument("--min-n", type=int, default=20)
    ap.add_argument("--top", type=int, default=30)
    a = ap.parse_args()
    rows = asyncio.run(_fetch(a.days))
    if a.what == "vsr":
        _print_vsr(rows)
    else:
        _print_recipes(rows, a.min_n, a.top)


if __name__ == "__main__":
    main()
