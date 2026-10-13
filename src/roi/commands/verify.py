"""`roi verify`: run the verification groups against the published warehouse."""

from __future__ import annotations

import re
from pathlib import Path

import click

from roi import verify_books
from roi.workdir import RoiError, WorkDir


@click.command("verify")
@click.option("--through", required=True, help="Last month to verify, YYYY-MM.")
@click.option("--work-dir", "work_dir", type=click.Path(file_okay=False, path_type=Path), default=None,
              help="Directory holding warehouse/; overrides the global --work-dir.")
@click.pass_obj
def command(work: WorkDir, through: str, work_dir: Path | None) -> None:
    """Verify the warehouse: (a) spend ties to the ledger to the cent."""
    if work_dir is not None:
        work = WorkDir(work_dir)
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", through):
        raise RoiError(2, f"bad month {through!r}; expected YYYY-MM")
    con = verify_books.open_warehouse(work)
    try:
        months = {m for (m,) in con.execute("SELECT DISTINCT strftime(month, '%Y-%m') FROM raw_output").fetchall()}
        if through not in months:
            raise RoiError(2, f"the books have no output for {through}")
        report = verify_books.check_gl_tie(con, through)
    finally:
        con.close()
    click.echo(f"verify through {through}")
    click.echo(
        f"(a) GL tie: ok, {report.team_months} team-months and {report.months} company-months tie to the cent,"
        " and the mart agrees"
    )
    click.echo("(b) interval coverage: not run, not available yet")
    click.echo("(c) unmeasured discipline: not run, not available yet")
