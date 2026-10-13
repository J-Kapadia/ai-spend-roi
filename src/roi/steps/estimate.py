"""Estimate the group-time effects and the team effects into the file being built.

Writes `est_att_gt`, `est_att_cov` and `est_team_effect` in one transaction. A team that is
not `measured` gets a status and a reason and nothing else: no effect, interval or cells.
"""

from __future__ import annotations

from datetime import date

import duckdb

from roi import estimator
from roi.steps import BuildContext

NAME = "estimate"
ORDER = 30

SCHEMA = (
    "CREATE TABLE est_att_gt(through_month VARCHAR, team_id VARCHAR, g DATE, t DATE, att DOUBLE,"
    " se DOUBLE, n_controls INTEGER)",
    "CREATE TABLE est_att_cov(through_month VARCHAR, team_id VARCHAR, g DATE, t1 DATE, t2 DATE, cov DOUBLE)",
    "CREATE TABLE est_team_effect(through_month VARCHAR, team_id VARCHAR, status VARCHAR, reason VARCHAR,"
    " effect DOUBLE, ci_lo DOUBLE, ci_hi DOUBLE, n_pre INTEGER, n_post INTEGER, df INTEGER,"
    " baseline_outcome DOUBLE, baseline_basis VARCHAR, pretrend_testable BOOLEAN, pretrend_p DOUBLE,"
    " estimator_version VARCHAR)",
)


def month_start(month: str) -> date:
    return date(int(month[:4]), int(month[5:]), 1)


def rows(through: str, panel: estimator.Panel) -> tuple[list, list, list]:
    att_rows, cov_rows, effect_rows = [], [], []
    for team, r in estimator.estimate(panel, through).items():
        a = r.analysis
        if r.measured:
            g = month_start(a.cells[0].g)
            for i, c in enumerate(a.cells):
                att_rows.append((through, team, g, month_start(c.t), c.att, c.se, c.n_controls))
                for j in range(i, len(a.cells)):
                    cov_rows.append((through, team, g, month_start(c.t), month_start(a.cells[j].t), float(a.cov[i, j])))
        testable = a.pretrend.testable if a is not None else None
        p_value = a.pretrend.p_value if a is not None else None
        effect_rows.append((
            through, team, r.status, r.reason, r.effect, r.ci_lo, r.ci_hi, r.n_pre, r.n_post, r.df,
            r.baseline_outcome, r.baseline_basis, testable, p_value, estimator.ESTIMATOR_VERSION,
        ))
    return att_rows, cov_rows, effect_rows


def run(ctx: BuildContext) -> None:
    con = duckdb.connect(str(ctx.db))
    try:
        att_rows, cov_rows, effect_rows = rows(ctx.through, estimator.read_panel(con, ctx.through))
        con.execute("BEGIN")
        for statement in SCHEMA:
            con.execute(statement)
        con.executemany("INSERT INTO est_att_gt VALUES (?, ?, ?, ?, ?, ?, ?)", att_rows)
        con.executemany("INSERT INTO est_att_cov VALUES (?, ?, ?, ?, ?, ?)", cov_rows)
        con.executemany("INSERT INTO est_team_effect VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", effect_rows)
        con.execute("COMMIT")
    finally:
        con.close()
