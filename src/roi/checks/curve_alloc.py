"""The fitted curves and the allocation cover measured teams only, and the allocation stays in its budget."""

from __future__ import annotations

import duckdb

from roi import estimator, verify_books
from roi.checks import CheckResult
from roi.workdir import WorkDir

NAME = "curve_alloc"
ORDER = 30
TABLES = ("fit_curve", "alloc_result")


def run(con: duckdb.DuckDBPyConnection, through: str, work: WorkDir) -> CheckResult:
    verify_books.require(con, *TABLES)
    est = verify_books.team_effects(con, through)
    measured = verify_books.teams_with_status(est, estimator.MEASURED)
    failures: list[str] = []
    for table in TABLES:
        for team in sorted(verify_books.teams_in(con, table, through) - measured):
            failures.append(f"team={team} is {est.get(team, {}).get('status', 'unknown')} and has a row in {table}")
    for team, amount in con.execute(
        f"SELECT team_id, amount_cents FROM ({verify_books.at('alloc_result')}) WHERE amount_cents < 0 ORDER BY team_id",
        [through],
    ).fetchall():
        failures.append(f"team={team} has a negative amount in alloc_result ({amount} cents)")
    for spent, budget in con.execute(
        f"SELECT sum(amount_cents)::BIGINT, max(budget_cents)::BIGINT FROM ({verify_books.at('alloc_result')})"
        " HAVING count(*) > 0", [through],
    ).fetchall():
        if spent > budget:
            failures.append(f"alloc_result allocates {spent} cents, more than its budget of {budget} cents")
    return CheckResult(NAME, tuple(failures))
