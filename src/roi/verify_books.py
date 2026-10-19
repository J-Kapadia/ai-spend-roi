"""Verification of the books, group (a): the ledger tie, and group (c): unmeasured discipline.
Reads no truth.

Spend and ledger sums are recomputed here straight from the `raw_*` tables, with SQL of
this module's own, and compared to each other and to the mart `fct_ai_spend_team_month`. A
mart that is stale, or that the models got wrong, cannot hide a difference in the books.
The company-month total is the sum of its team-month rows (the `unassigned` bucket is a team
here), so a total can only differ if some team-month does.

Group (c) is a registry of one module per check in `roi.checks`, run in order and printed by
name: each team's status and reason recomputed from the books, no figure or ROI, curve or
allocation row for a team that is not measured, the ROI rows of every measured team, an agenda
item for each unmeasured team and no other, a warning on unassigned spend, and the return table
of a built site. A table that a check needs and the warehouse lacks is a failure of the build
(exit 3), never a skip.

The leave-one-control-out report re-estimates each measured team without each of its control
teams in turn, where it has at least two. It is a report, not a check that fails the run.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import duckdb

from roi.db import connect as db_connect
from roi import estimator
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
    return db_connect(work.warehouse_db, read_only=True)


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


# ---------------------------------------------------------------------------------------------
# Group (c): unmeasured discipline

@dataclass(frozen=True)
class TeamStatus:
    team_id: str
    status: str
    reason: str | None


@dataclass(frozen=True)
class DisciplineReport:
    through: str
    teams: tuple[TeamStatus, ...]
    checked: tuple[str, ...]  # the ids of the checks that ran
    failures: tuple[str, ...]
    warnings: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()  # what a check accepted or did not do, and why

    def with_status(self, status: str) -> list[TeamStatus]:
        return [t for t in self.teams if t.status == status]


def require(con: duckdb.DuckDBPyConnection, *tables: str) -> None:
    """Raise RoiError(3, ...) naming the first of `tables` that the warehouse lacks."""
    present = {t for (t,) in con.execute("SELECT table_name FROM information_schema.tables").fetchall()}
    for table in tables:
        if table not in present:
            raise RoiError(3, f"the warehouse has no {table}; rebuild it")


def at(table: str) -> str:
    """Rows of a result table for one through month, whatever the type of its through_month."""
    return f"SELECT * FROM {table} WHERE substr(CAST(through_month AS VARCHAR), 1, 7) = ?"


def teams_in(con: duckdb.DuckDBPyConnection, table: str, through: str) -> set[str]:
    return {t for (t,) in con.execute(f"SELECT DISTINCT team_id FROM ({at(table)})", [through]).fetchall()}


def team_effects(con: duckdb.DuckDBPyConnection, through: str) -> dict[str, dict]:
    """The row of est_team_effect of each team for `through`, by team. Raises RoiError(3, ...) when
    there is no table and RoiError(2, ...) when the warehouse holds no results for `through`."""
    require(con, "est_team_effect")
    results = con.execute(at("est_team_effect"), [through]).fetchall()
    if not results:
        (built,) = con.execute("SELECT max(through_month) FROM est_team_effect").fetchone()
        raise RoiError(2, f"the warehouse holds results through {built}, not {through}; run `roi build`")
    cols = [d[0] for d in con.description]
    return {r[cols.index("team_id")]: dict(zip(cols, r)) for r in results}


def teams_with_status(effects: dict[str, dict], status: str) -> set[str]:
    return {t for t, r in effects.items() if r["status"] == status}


def result_months(con: duckdb.DuckDBPyConnection) -> list[str]:
    """The through months the estimator has results for. Raises RoiError(3, ...) when there is no table."""
    require(con, "est_team_effect")
    return [m for (m,) in con.execute("SELECT DISTINCT through_month FROM est_team_effect ORDER BY 1").fetchall()]


def check_unmeasured_discipline(con: duckdb.DuckDBPyConnection, through: str, work: WorkDir) -> DisciplineReport:
    """Group (c): run every registered check of `roi.checks`. Raises RoiError(3, ...) when a table of
    the pipeline is absent and RoiError(2, ...) when the warehouse holds no results for `through`;
    otherwise returns the failures and warnings found."""
    from roi import checks  # not at the top: the checks import this module

    effects = team_effects(con, through)
    results = [module.run(con, through, work) for module in checks.discover()]
    return DisciplineReport(
        through=through,
        teams=tuple(TeamStatus(t, effects[t]["status"], effects[t]["reason"]) for t in sorted(effects)),
        checked=tuple(r.id for r in results),
        failures=tuple(f for r in results for f in r.failures),
        warnings=tuple(w for r in results for w in r.warnings),
        notes=tuple(r.note for r in results if r.note),
    )


def allocation_degenerate(con: duckdb.DuckDBPyConnection, through: str) -> str | None:
    """The warning text when the allocation for `through` is degenerate: fewer than two teams funded,
    or none of the funded teams uncapped. A property of the data, not a failed check. None when the
    allocation is fine or was not written (no budget configured)."""
    columns = {c for (c,) in con.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'alloc_result'").fetchall()}
    if not {"through_month", "amount_cents", "capped"} <= columns:
        return None  # not built, or not the allocation table of this pipeline
    rows = con.execute(f"SELECT amount_cents, capped FROM ({at('alloc_result')})", [through]).fetchall()
    if not rows:
        return None
    funded = [capped for amount, capped in rows if amount > 0]
    uncapped = [c for c in funded if not c]
    reasons = []
    if len(funded) < 2:
        reasons.append(f"{len(funded)} funded team{'' if len(funded) == 1 else 's'}, fewer than two")
    if not uncapped:
        reasons.append("none of the funded teams is uncapped")
    return "allocation degenerate: " + "; ".join(reasons) if reasons else None


# ---------------------------------------------------------------------------------------------
# Leave-one-control-out

@dataclass(frozen=True)
class LeaveOut:
    dropped: str
    effect: float | None  # None when the team is not estimable without this control
    cells: int  # cells left


@dataclass(frozen=True)
class ControlSensitivity:
    team_id: str
    effect: float
    ci_lo: float
    ci_hi: float
    controls: tuple[str, ...]  # every control any of the team's cells uses
    runs: tuple[LeaveOut, ...] = field(default=())  # empty: fewer than two controls

    @property
    def single_control(self) -> bool:
        return len(self.controls) < 2


def leave_one_control_out(panel: estimator.Panel, through: str) -> list[ControlSensitivity]:
    """Re-estimate every measured team without each of its control teams in turn.

    A control is any team that serves in one of the team's cells; dropping it removes it from the
    whole panel, so a cell that had only that control is lost and the team may no longer be
    estimable. A team with fewer than two controls is not run (it would have none left).
    """
    out = []
    for team, result in estimator.estimate(panel, through).items():
        if not result.measured:
            continue
        controls = tuple(sorted({c for cell in result.analysis.cells for c in cell.controls}))
        runs = []
        if len(controls) >= 2:
            for dropped in controls:
                reduced = estimator.Panel(
                    {t: v for t, v in panel.y.items() if t != dropped},
                    {t: g for t, g in panel.rollout.items() if t != dropped},
                    {t: v for t, v in panel.outcome.items() if t != dropped},
                )
                a = estimator.analyse(reduced, team, through)
                usable = a is not None and len(a.cells) >= estimator.MIN_POST and a.computable
                runs.append(LeaveOut(dropped, a.effect if usable else None, len(a.cells) if a is not None else 0))
        out.append(ControlSensitivity(team, result.effect, result.ci_lo, result.ci_hi, controls, tuple(runs)))
    return out
