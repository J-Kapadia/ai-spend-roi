"""`roi estimate`: run the estimator against the built warehouse and print the team effects."""

from __future__ import annotations

import json
import re

import click
import duckdb

from roi import estimator
from roi.workdir import RoiError, WorkDir


def _fmt(x: float | None) -> str:
    return "-" if x is None else f"{x:+.4f}"


@click.command("estimate")
@click.option("--through", help="Last month to use, YYYY-MM. Defaults to the last month in the warehouse.")
@click.option("--json", "as_json", is_flag=True, help="Print JSON instead of a table.")
@click.pass_obj
def command(work: WorkDir, through: str | None, as_json: bool) -> None:
    """Print each team's status and effect (log points). Opens the warehouse read-only."""
    if through is not None and not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", through):
        raise RoiError(2, f"bad month {through!r}; expected YYYY-MM")
    if not work.warehouse_db.exists():
        raise RoiError(2, f"no warehouse at {work.warehouse_db}; run `roi build` first")
    con = duckdb.connect(str(work.warehouse_db), read_only=True)
    try:
        if through is None:
            (last,) = con.execute("SELECT strftime(max(month), '%Y-%m') FROM fct_team_month").fetchone()
            through = last
        panel = estimator.read_panel(con, through)
    finally:
        con.close()
    if not panel.y:
        raise RoiError(2, f"no panel rows through {through}")
    results = estimator.estimate(panel, through)
    if as_json:
        click.echo(json.dumps({
            "through_month": through,
            "estimator_version": estimator.ESTIMATOR_VERSION,
            "teams": {
                t: {"status": r.status, "reason": r.reason, "effect": r.effect, "ci_lo": r.ci_lo, "ci_hi": r.ci_hi,
                    "n_pre": r.n_pre, "n_post": r.n_post, "df": r.df}
                for t, r in results.items()
            },
        }, indent=2))
        return
    click.echo(f"estimator {estimator.ESTIMATOR_VERSION}, through {through}, effects in log points")
    click.echo(
        f"{'team':<24}{'status':<12}{'reason':<15}{'effect':>9}{'ci_lo':>9}{'ci_hi':>9}{'pre':>5}{'post':>6}  pre-trend p"
    )
    for team, r in results.items():
        pre_trend = "-"
        if r.analysis is not None:
            p = r.analysis.pretrend.p_value
            pre_trend = "not testable" if p is None else f"{p:.3f}"
        click.echo(
            f"{team:<24}{r.status:<12}{r.reason or '-':<15}{_fmt(r.effect):>9}{_fmt(r.ci_lo):>9}"
            f"{_fmt(r.ci_hi):>9}{r.n_pre:>5}{r.n_post:>6}  {pre_trend}"
        )
