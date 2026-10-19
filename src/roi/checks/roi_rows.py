"""Every measured team has its return rows, and no other team has one."""

from __future__ import annotations

import duckdb

from roi import estimator, verify_books
from roi.checks import CheckResult
from roi.workdir import WorkDir

NAME = "roi_rows"
ORDER = 20
TABLES = ("roi_team", "roi_sensitivity")


def run(con: duckdb.DuckDBPyConnection, through: str, work: WorkDir) -> CheckResult:
    verify_books.require(con, *TABLES)
    est = verify_books.team_effects(con, through)
    measured = verify_books.teams_with_status(est, estimator.MEASURED)
    failures: list[str] = []
    for table in TABLES:
        have = verify_books.teams_in(con, table, through)
        for team in sorted(have - measured):
            failures.append(f"team={team} is {est.get(team, {}).get('status', 'unknown')} and has a row in {table}")
        for team in sorted(measured - have):
            failures.append(f"team={team} is measured and has no row in {table}")
    return CheckResult(NAME, tuple(failures))
