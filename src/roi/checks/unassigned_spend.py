"""A warning when a month has more than 5% of its company spend in the `unassigned` bucket."""

from __future__ import annotations

import duckdb

from roi import verify_books
from roi.checks import CheckResult
from roi.workdir import WorkDir

NAME = "unassigned_spend"
ORDER = 50
THRESHOLD = 0.05
BUCKET = "unassigned"


def run(con: duckdb.DuckDBPyConnection, through: str, work: WorkDir) -> CheckResult:
    verify_books.require(con, "fct_ai_spend_team_month")
    rows = con.execute(
        "SELECT strftime(month, '%Y-%m') AS m, sum(spend_cents) FILTER (WHERE team_id = ?)::BIGINT, sum(spend_cents)::BIGINT"
        " FROM fct_ai_spend_team_month WHERE strftime(month, '%Y-%m') <= ? GROUP BY 1 ORDER BY 1",
        [BUCKET, through],
    ).fetchall()
    warnings = tuple(
        f"unassigned spend is {unassigned / total:.1%} of company spend in {month}"
        for month, unassigned, total in rows
        if unassigned and total > 0 and unassigned / total > THRESHOLD
    )
    return CheckResult(NAME, warnings=warnings)
