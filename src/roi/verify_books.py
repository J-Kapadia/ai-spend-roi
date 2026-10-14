"""Verification of the books, group (a): the ledger tie, and group (c): unmeasured discipline.
Reads no truth.

Spend and ledger sums are recomputed here straight from the `raw_*` tables, with SQL of
this module's own, and compared to each other and to the mart `fct_ai_spend_team_month`. A
mart that is stale, or that the models got wrong, cannot hide a difference in the books.
The company-month total is the sum of its team-month rows (the `unassigned` bucket is a team
here), so a total can only differ if some team-month does.

Group (c) recomputes each team's status from the books (first month with spend, months with
output) and checks the result tables against it: an unmeasured or control team has a status
and a reason and no effect, interval, ROI, curve or allocation row, every measured team has its
ROI rows, and an agenda, where one has been built, has an item for each unmeasured team and for
no other (an agenda written empty is accepted only when the caller says the build had no config to
write it from, and is reported as empty). A table that a later step adds is checked once it exists
and is reported by name as not built until then.

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

# Tables that must exist (exit 3 when absent), and tables checked once a later step builds them.
REQUIRED_TABLES = ("est_team_effect", "est_att_gt", "roi_team", "roi_sensitivity")
MEASURED_ONLY_TABLES = ("roi_team", "roi_sensitivity", "fit_curve", "alloc_result")
AGENDA_TABLE = "agenda_item"
LATER_TABLES = ("fit_curve", "alloc_result", AGENDA_TABLE)


@dataclass(frozen=True)
class TeamStatus:
    team_id: str
    status: str
    reason: str | None


@dataclass(frozen=True)
class DisciplineReport:
    through: str
    teams: tuple[TeamStatus, ...]
    checked: tuple[str, ...]  # tables the checks looked at
    not_built: tuple[str, ...]  # later tables that do not exist yet
    failures: tuple[str, ...]
    empty: tuple[str, ...] = ()  # tables that exist and were accepted with no rows

    def with_status(self, status: str) -> list[TeamStatus]:
        return [t for t in self.teams if t.status == status]


def _tables(con: duckdb.DuckDBPyConnection) -> set[str]:
    return {t for (t,) in con.execute("SELECT table_name FROM information_schema.tables").fetchall()}


def _at(table: str) -> str:
    """Rows of a result table for one through month, whatever the type of its through_month."""
    return f"SELECT * FROM {table} WHERE substr(CAST(through_month AS VARCHAR), 1, 7) = ?"


def _expected(g: str | None, outputs: list[str]) -> tuple[str, str | None, int, int]:
    """(status, reason or None for the statuses that need the estimator, n_pre, n_post) from the books."""
    if g is None:
        return estimator.CONTROL, None, len(outputs), 0
    n_pre = sum(m < g for m in outputs)
    n_post = len(outputs) - n_pre
    if n_pre < estimator.MIN_PRE:
        return estimator.UNMEASURED, estimator.NO_PRE_PERIOD, n_pre, n_post
    if n_post < estimator.MIN_POST:
        return estimator.UNMEASURED, estimator.TOO_RECENT, n_pre, n_post
    return estimator.MEASURED, None, n_pre, n_post


def result_months(con: duckdb.DuckDBPyConnection) -> list[str]:
    """The through months the estimator has results for. Raises RoiError(3, ...) when there is no table."""
    try:
        return [m for (m,) in con.execute("SELECT DISTINCT through_month FROM est_team_effect ORDER BY 1").fetchall()]
    except duckdb.CatalogException:
        raise RoiError(3, "the warehouse has no est_team_effect; rebuild it") from None


def check_unmeasured_discipline(
    con: duckdb.DuckDBPyConnection, through: str, *, empty_agenda_ok: bool = False
) -> DisciplineReport:
    """Group (c). Raises RoiError(3, ...) when a table of the pipeline is absent and RoiError(2, ...)
    when the warehouse holds no results for `through`; otherwise returns the failures found.

    `empty_agenda_ok` accepts an agenda with no rows at all, for a build that had no config to write
    one from; with it unset an empty agenda fails for every unmeasured team."""
    present = _tables(con)
    for table in REQUIRED_TABLES:
        if table not in present:
            raise RoiError(3, f"the warehouse has no {table}; rebuild it")
    results = con.execute(_at("est_team_effect"), [through]).fetchall()
    if not results:
        (built,) = con.execute("SELECT max(through_month) FROM est_team_effect").fetchone()
        raise RoiError(2, f"the warehouse holds results through {built}, not {through}; run `roi build`")
    cols = [d[0] for d in con.description]
    est = {r[cols.index("team_id")]: dict(zip(cols, r)) for r in results}

    first_spend = dict(con.execute(
        "SELECT team_id, min(m) FROM (SELECT team_id, strftime(usage_date, '%Y-%m') AS m FROM raw_ai_spend"
        " GROUP BY 1, 2 HAVING sum(cost_cents) > 0) GROUP BY 1"
    ).fetchall())
    outputs: dict[str, list[str]] = {}
    for team, month in con.execute(
        "SELECT team_id, strftime(month, '%Y-%m') FROM raw_output WHERE value > 0 AND strftime(month, '%Y-%m') <= ?"
        " ORDER BY 1, 2", [through],
    ).fetchall():
        outputs.setdefault(team, []).append(month)

    failures: list[str] = []
    for team in sorted(set(outputs) | set(est)):
        row = est.get(team)
        if row is None:
            failures.append(f"team={team} has output in the books and no row in est_team_effect")
            continue
        want, want_reason, n_pre, n_post = _expected(first_spend.get(team), outputs.get(team, []))
        status, reason = row["status"], row["reason"]
        if row["n_pre"] != n_pre:
            failures.append(f"team={team} n_pre={row['n_pre']} but the books have {n_pre} months before rollout")
        if want == estimator.CONTROL:
            ok = status == estimator.CONTROL and reason is None
        elif want_reason is not None:
            ok = status == estimator.UNMEASURED and reason == want_reason
        else:  # a team with a window: the estimator decides among measured and the three data-driven reasons
            ok = (status == estimator.MEASURED and reason is None) or (
                status == estimator.UNMEASURED
                and reason in (estimator.NO_COMPARISON, estimator.PRE_TREND, estimator.VARIANCE_NOT_COMPUTABLE)
            )
        if not ok:
            failures.append(
                f"team={team} status={status} reason={reason} but the books say "
                f"{want}{'/' + want_reason if want_reason else ''} (pre={n_pre}, post={n_post})"
            )
        if status == estimator.UNMEASURED and reason == estimator.PRE_TREND and not (
            row["pretrend_testable"] and row["pretrend_p"] is not None and row["pretrend_p"] < estimator.PRETREND_ALPHA
        ):
            failures.append(f"team={team} is unmeasured (pre_trend) without a significant pre-trend test")
        if status == estimator.MEASURED:
            if None in (row["effect"], row["ci_lo"], row["ci_hi"]) or not row["ci_lo"] <= row["effect"] <= row["ci_hi"]:
                failures.append(f"team={team} is measured without a well-formed effect and interval")
            if row["pretrend_testable"] and row["pretrend_p"] < estimator.PRETREND_ALPHA:
                failures.append(f"team={team} is measured with a significant pre-trend (p={row['pretrend_p']:.4f})")
        elif any(row[c] is not None for c in ("effect", "ci_lo", "ci_hi")):
            failures.append(f"team={team} is {status} and carries an effect or interval")

    measured = {t for t, r in est.items() if r["status"] == estimator.MEASURED}
    unmeasured = {t for t, r in est.items() if r["status"] == estimator.UNMEASURED}
    checked = ["est_team_effect", "est_att_gt"]

    def teams_in(table: str) -> set[str]:
        return {t for (t,) in con.execute(f"SELECT DISTINCT team_id FROM ({_at(table)})", [through]).fetchall()}

    for team in sorted(teams_in("est_att_gt") - measured):
        failures.append(f"team={team} is not measured and has group-time cells in est_att_gt")
    for table in MEASURED_ONLY_TABLES:
        if table not in present:
            continue
        checked.append(table)
        for team in sorted(teams_in(table) - measured):
            failures.append(f"team={team} is {est.get(team, {}).get('status', 'unknown')} and has a row in {table}")
    for table in ("roi_team", "roi_sensitivity"):
        for team in sorted(measured - teams_in(table)):
            failures.append(f"team={team} is measured and has no row in {table}")
    empty: list[str] = []
    if AGENDA_TABLE in present:
        have = teams_in(AGENDA_TABLE)
        if not have and empty_agenda_ok:
            empty.append(AGENDA_TABLE)
        else:
            checked.append(AGENDA_TABLE)
            for team in sorted(unmeasured - have):
                failures.append(f"team={team} is unmeasured and has no item in {AGENDA_TABLE}")
            for team in sorted(have - unmeasured):
                status = est.get(team, {}).get("status", "unknown")
                failures.append(f"team={team} is {status} and has an item in {AGENDA_TABLE}")

    return DisciplineReport(
        through=through,
        teams=tuple(TeamStatus(t, est[t]["status"], est[t]["reason"]) for t in sorted(est)),
        checked=tuple(checked),
        not_built=tuple(t for t in LATER_TABLES if t not in present),
        failures=tuple(failures),
        empty=tuple(empty),
    )


def allocation_degenerate(con: duckdb.DuckDBPyConnection, through: str) -> str | None:
    """The warning text when the allocation for `through` is degenerate: fewer than two teams funded,
    or none of the funded teams uncapped. A property of the data, not a failed check. None when the
    allocation is fine or was not written (no budget configured)."""
    columns = {c for (c,) in con.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'alloc_result'").fetchall()}
    if not {"through_month", "amount_cents", "capped"} <= columns:
        return None  # not built, or not the allocation table of this pipeline
    rows = con.execute(f"SELECT amount_cents, capped FROM ({_at('alloc_result')})", [through]).fetchall()
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
