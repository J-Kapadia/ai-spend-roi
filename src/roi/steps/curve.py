"""Fit each measured team's dose-response curve into the file being built.

Writes `fit_curve` in one transaction: one row per measured team, identified or not. A team
that is not measured has no row.
"""

from __future__ import annotations

import duckdb

from roi import curve
from roi import steps
from roi.steps import BuildContext

NAME = "curve"
ORDER = 52

SCHEMA = (
    "CREATE TABLE fit_curve(through_month VARCHAR, team_id VARCHAR, a DOUBLE, k DOUBLE, identified BOOLEAN,"
    " marginal_at_current DOUBLE, marginal_se DOUBLE)"
)


def rows(con: duckdb.DuckDBPyConnection, through: str) -> list[tuple]:
    profile = curve.spend_profile(con, through)
    teams = [t for (t,) in con.execute(
        "SELECT team_id FROM est_team_effect WHERE through_month = ? AND status = 'measured' ORDER BY team_id",
        [through],
    ).fetchall()]
    out = []
    for team in teams:
        f = curve.fit_team(con, through, team, profile[team][0])
        out.append((through, team, f.a, f.k, f.identified, f.marginal_at_current, f.marginal_se))
    return out


def run(ctx: BuildContext) -> None:
    con = duckdb.connect(str(ctx.db))
    try:
        fitted = rows(con, ctx.through)
        con.execute("BEGIN")
        steps.create_table(con, SCHEMA)
        if fitted:
            con.executemany("INSERT INTO fit_curve VALUES (?, ?, ?, ?, ?, ?, ?)", fitted)
        con.execute("COMMIT")
    finally:
        con.close()
