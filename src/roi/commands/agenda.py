"""`roi agenda`: print the testing agenda of the unmeasured teams."""

from __future__ import annotations

import json
import re

import click

from roi import agenda, alloc
from roi.workdir import RoiError, WorkDir

FIXTURE = alloc.FIXTURES / "agenda_small.json"


@click.command("agenda")
@click.option("--check", is_flag=True, help="Fixture only: run the rows of tests/fixtures/agenda_small.json.")
@click.option("--through", help="Month to design at, YYYY-MM. Defaults to the last month in the warehouse.")
@click.option("--json", "as_json", is_flag=True, help="Print JSON instead of text.")
@click.pass_obj
def command(work: WorkDir, check: bool, through: str | None, as_json: bool) -> None:
    """Print one designed experiment per unmeasured team. Opens the warehouse read-only."""
    if check:
        checks = agenda.check_fixture(agenda.load_fixture(FIXTURE))
        passed = all(ok for _, ok, _ in checks)
        if as_json:
            click.echo(json.dumps({
                "command": "agenda", "status": "pass" if passed else "fail",
                "checks": [{"id": i, "pass": ok, "detail": d} for i, ok, d in checks], "artifacts": [],
            }, indent=2))
        else:
            for ident, ok, detail in checks:
                click.echo(f"{'ok  ' if ok else 'FAIL'} {ident}: {detail}")
            if passed:
                click.echo("agenda: matches reference")
        if not passed:
            raise RoiError(1, "agenda differs from the reference")
        return

    if through is not None and not re.fullmatch(alloc.MONTH_PATTERN, through):
        raise RoiError(2, f"bad month {through!r}; expected YYYY-MM")
    cfg = alloc.load_config(alloc.config_path(work))
    if cfg is None:
        raise RoiError(2, f"no value per unit: {alloc.config_path(work)} is missing")
    con = alloc.open_readonly(work)
    try:
        through = through or alloc.last_month(con)
        items = agenda.items_from_warehouse(con, through, cfg)
    finally:
        con.close()
    if as_json:
        click.echo(json.dumps({
            "command": "agenda", "status": "pass", "checks": [], "artifacts": [],
            "result": {"through_month": through, "items": [i.__dict__ for i in items]},
        }, indent=2))
        return
    click.echo(f"testing agenda through {through}; rho and any sigma override are config assumptions")
    click.echo(f"{'team':<24}{'design':<20}{'n/arm':>7}{'months':>8}  note")
    for i in items:
        n = "-" if i.n_per_arm is None else str(i.n_per_arm)
        length = "-" if i.length_months is None else str(i.length_months)
        click.echo(f"{i.team_id:<24}{i.design:<20}{n:>7}{length:>8}  {i.note}")
