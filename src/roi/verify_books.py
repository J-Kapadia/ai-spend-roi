"""Verification of the books, group (a): the ledger tie. Reads no truth.

Spend and ledger sums are recomputed here straight from the `raw_*` tables, with SQL of
this module's own, and compared to each other and to the mart `fct_ai_spend_team_month`. A
mart that is stale, or that the models got wrong, cannot hide a difference in the books.
The company-month total is the sum of its team-month rows (the `unassigned` bucket is a team
here), so a total can only differ if some team-month does.
"""

from __future__ import annotations

from dataclasses import dataclass

import duckdb

from roi.workdir import RoiError, WorkDir

MAX_LINES = 10

# Per (team, month) up to the through month: spend from the export and the AI-usage line of the
# ledger, both straight from the raw tables.
BOOKS_SQL = """
WITH s AS (
    SELECT team_id, strftime(usage_date, '%Y-%m') AS m, sum(cost_cents)::BIGINT AS c
    FROM raw_ai_spend GROUP BY 1, 2
), g AS (
    SELECT cost_center AS team_id, period AS m, sum(debit_cents - credit_cents)::BIGINT AS c
    FROM raw_gl_journal WHERE account_code = ? GROUP BY 1, 2
)
SELECT coalesce(s.team_id, g.team_id) AS team_id, coalesce(s.m, g.m) AS m,
       coalesce(s.c, 0)::BIGINT AS spend_cents, coalesce(g.c, 0)::BIGINT AS gl_cents
FROM s FULL JOIN g ON s.team_id = g.team_id AND s.m = g.m
WHERE coalesce(s.m, g.m) <= ?
ORDER BY m, team_id
"""

MART_SQL = """
SELECT team_id, strftime(month, '%Y-%m') AS m, spend_cents, gl_cents, tie_diff_cents
FROM fct_ai_spend_team_month WHERE strftime(month, '%Y-%m') <= ?
"""


@dataclass(frozen=True)
class TieReport:
    through: str
    team_months: int
    months: int


def open_warehouse(work: WorkDir) -> duckdb.DuckDBPyConnection:
    if not work.warehouse_db.exists():
        raise RoiError(2, f"no warehouse at {work.warehouse_db}; run `roi build` first")
    return duckdb.connect(str(work.warehouse_db), read_only=True)


def _gl_account(con: duckdb.DuckDBPyConnection) -> str:
    try:
        row = con.execute("SELECT value FROM roi_settings WHERE key = 'gl_account'").fetchone()
    except duckdb.CatalogException:
        row = None
    if row is None:
        raise RoiError(3, "the warehouse does not record its ledger account of AI usage; rebuild it")
    return row[0]


def tie_failures(con: duckdb.DuckDBPyConnection, through: str) -> tuple[list[str], int, int]:
    """Return (failure messages, team-months checked, months checked) for months up to `through`."""
    books = con.execute(BOOKS_SQL, [_gl_account(con), through]).fetchall()
    failures = [
        f"GL tie failed: team={team} month={month} spend={spend} gl={gl} diff={spend - gl}"
        for team, month, spend, gl in books
        if spend != gl
    ]

    try:
        mart = {(t, m): (s, g, d) for t, m, s, g, d in con.execute(MART_SQL, [through]).fetchall()}
    except duckdb.CatalogException:
        raise RoiError(3, "the warehouse has no fct_ai_spend_team_month; rebuild it") from None
    recomputed = {(t, m): (s, g, s - g) for t, m, s, g in books}
    for key in sorted(mart.keys() | recomputed.keys(), key=lambda k: (k[1], k[0])):
        got, want = mart.get(key, (0, 0, 0)), recomputed.get(key, (0, 0, 0))
        if got != want:
            failures.append(
                f"mart disagrees with the books: team={key[0]} month={key[1]} "
                f"mart spend={got[0]} gl={got[1]} diff={got[2]}, books spend={want[0]} gl={want[1]} diff={want[2]}"
            )
    return failures, len(books), len({m for _, m, _, _ in books})


def check_gl_tie(con: duckdb.DuckDBPyConnection, through: str) -> TieReport:
    """Group (a). Raises RoiError(3, ...) naming the first team-months that do not tie."""
    failures, team_months, months = tie_failures(con, through)
    if failures:
        shown = failures[:MAX_LINES]
        if len(failures) > MAX_LINES:
            shown.append(f"... and {len(failures) - MAX_LINES} more")
        raise RoiError(3, "\n".join(shown))
    return TieReport(through=through, team_months=team_months, months=months)
