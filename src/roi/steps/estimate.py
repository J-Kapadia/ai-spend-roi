"""STUB ESTIMATOR, THROWAWAY (plan Phase 0). Not the Callaway-Sant'Anna estimator of D-04.

A plain pre/post difference in the log outcome, minus the same difference for the never
treated team(s). No standard errors and no pre-trend test, so it reports no interval.
Phase 3 replaces this file with the real estimator.
"""

from __future__ import annotations

from collections import defaultdict

import duckdb

from roi.steps import BuildContext

NAME = "estimate"
ORDER = 30
ESTIMATOR_VERSION = "stub-throwaway-0"


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs)


def run(ctx: BuildContext) -> None:
    con = duckdb.connect(str(ctx.db))
    try:
        rows = con.execute(
            "SELECT team_id, month, y_log, rollout_month FROM fct_team_month"
            " WHERE strftime(month, '%Y-%m') <= ? AND y_log IS NOT NULL ORDER BY team_id, month",
            [ctx.through],
        ).fetchall()
        y: dict[str, dict] = defaultdict(dict)
        rollout: dict[str, object] = {}
        for team_id, month, y_log, rollout_month in rows:
            y[team_id][month] = y_log
            rollout[team_id] = rollout_month
        controls = [t for t, g in rollout.items() if g is None]
        control_mean = {
            m: _mean([y[c][m] for c in controls if m in y[c]])
            for m in sorted({m for c in controls for m in y[c]})
        }

        out = []
        for team_id in sorted(y):
            g = rollout[team_id]
            if g is None:
                out.append((ctx.through, team_id, "control", None, None, len(y[team_id]), 0))
                continue
            gap = {m: v - control_mean[m] for m, v in y[team_id].items() if m in control_mean}
            pre = [v for m, v in gap.items() if m < g]
            post = [v for m, v in gap.items() if m >= g]
            if not controls:
                out.append((ctx.through, team_id, "unmeasured", "no_comparison", None, len(pre), len(post)))
            elif not pre:
                out.append((ctx.through, team_id, "unmeasured", "no_pre_period", None, 0, len(post)))
            elif not post:
                out.append((ctx.through, team_id, "unmeasured", "too_recent", None, len(pre), 0))
            else:
                out.append((ctx.through, team_id, "measured", None, _mean(post) - _mean(pre), len(pre), len(post)))

        con.execute("BEGIN")
        con.execute(
            "CREATE TABLE est_team_effect(through_month VARCHAR, team_id VARCHAR, status VARCHAR,"
            " reason VARCHAR, effect DOUBLE, n_pre INTEGER, n_post INTEGER, estimator_version VARCHAR)"
        )
        con.executemany(
            "INSERT INTO est_team_effect VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [(*r, ESTIMATOR_VERSION) for r in out],
        )
        con.execute("COMMIT")
    finally:
        con.close()
