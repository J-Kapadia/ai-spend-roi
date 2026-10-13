"""`roi allocate`: allocate a monthly budget increment from the built warehouse."""

from __future__ import annotations

import json
import re

import click
import duckdb

from roi import alloc
from roi.workdir import RoiError, WorkDir

FIXTURE = alloc.FIXTURES / "alloc_small.json"


def _emit(as_json: bool, status: str, checks: list[tuple[str, bool, str]], result: dict | None = None) -> None:
    if as_json:
        out = {"command": "allocate", "status": status,
               "checks": [{"id": i, "pass": ok, "detail": d} for i, ok, d in checks], "artifacts": []}
        if result is not None:
            out["result"] = result
        click.echo(json.dumps(out, indent=2))


@click.command("allocate")
@click.option("--budget", required=True, type=click.IntRange(min=0), help="Monthly increment, whole USD.")
@click.option("--check", is_flag=True,
              help="Fixture only: compare with tests/fixtures/alloc_small.json, whose budget_usd it must equal.")
@click.option("--through", help="Month to allocate at, YYYY-MM. Defaults to the last month in the warehouse.")
@click.option("--json", "as_json", is_flag=True, help="Print JSON instead of text.")
@click.pass_obj
def command(work: WorkDir, budget: int, check: bool, through: str | None, as_json: bool) -> None:
    """Allocate BUDGET dollars a month across the measured teams. Opens the warehouse read-only."""
    if check:
        checks = alloc.check_fixture(alloc.load_fixture(FIXTURE), budget)
        passed = all(ok for _, ok, _ in checks)
        _emit(as_json, "pass" if passed else "fail", checks)
        if not as_json:
            for ident, ok, detail in checks:
                click.echo(f"{'ok  ' if ok else 'FAIL'} {ident}: {detail}")
            if passed:
                click.echo("allocation: matches reference")
        if not passed:
            raise RoiError(1, "allocation differs from the reference")
        return

    if through is not None and not re.fullmatch(alloc.MONTH_PATTERN, through):
        raise RoiError(2, f"bad month {through!r}; expected YYYY-MM")
    cfg = alloc.load_config(alloc.config_path(work))
    if cfg is None:
        raise RoiError(2, f"no value per unit: {alloc.config_path(work)} is missing")
    con = alloc.open_readonly(work)
    try:
        through = through or alloc.last_month(con)
        try:
            teams = alloc.teams_from_warehouse(con, through, cfg)
        except duckdb.CatalogException:
            raise RoiError(2, "the warehouse has no fit_curve; run `roi build` first") from None
    finally:
        con.close()
    result = alloc.allocate(teams, budget * 100)
    if as_json:
        _emit(True, "pass", [], {
            "through_month": through, "budget_cents": result.budget_cents,
            "unallocated_cents": result.unallocated_cents,
            "teams": {r.team_id: {"amount_cents": r.amount_cents, "capped": r.capped,
                                  "funded_share_in_draws": r.funded_share_in_draws,
                                  "beyond_observed_share": r.beyond_observed_share} for r in result.rows},
        })
        return
    click.echo(f"allocation of ${budget:,} a month, through {through}")
    click.echo(f"{'team':<24}{'amount':>14}{'capped':>8}{'funded in draws':>17}{'beyond observed':>17}{'return per $1':>15}")
    for r in result.rows:
        m = "-" if r.marginal_return is None else f"{r.marginal_return:.4f}"
        click.echo(
            f"{r.team_id:<24}{r.amount_cents / 100:>14,.2f}{'yes' if r.capped else 'no':>8}"
            f"{r.funded_share_in_draws:>17.1%}{r.beyond_observed_share:>17.1%}{m:>15}"
        )
    click.echo(f"unallocated: ${result.unallocated_cents / 100:,.2f}")
