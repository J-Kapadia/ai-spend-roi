"""Each team's status and reason follow the books, and a team without a measurement has none."""

from __future__ import annotations

import duckdb

from roi import estimator, verify_books
from roi.checks import CheckResult
from roi.workdir import WorkDir

NAME = "status_rule"
ORDER = 10


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


def run(con: duckdb.DuckDBPyConnection, through: str, work: WorkDir) -> CheckResult:
    est = verify_books.team_effects(con, through)
    verify_books.require(con, "est_att_gt")
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

    measured = verify_books.teams_with_status(est, estimator.MEASURED)
    for team in sorted(verify_books.teams_in(con, "est_att_gt", through) - measured):
        failures.append(f"team={team} is not measured and has group-time cells in est_att_gt")
    return CheckResult(NAME, tuple(failures))
