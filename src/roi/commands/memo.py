"""`roi memo`: render the static site; with --check, verify every number resolves."""

from __future__ import annotations

import re

import click

from roi import memo
from roi.workdir import RoiError, WorkDir


@click.command("memo")
@click.option("--month", required=True, help="Month to report, YYYY-MM.")
@click.option("--check", is_flag=True, help="Re-run every provenance predicate and compare.")
@click.pass_obj
def command(work: WorkDir, month: str, check: bool) -> None:
    """Build the memo site into site/."""
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        raise RoiError(2, f"bad month {month!r}; expected YYYY-MM")
    con = memo.open_warehouse(work)
    try:
        index = memo.build_site(work, month, con)
        click.echo(f"wrote {index}")
        if check:
            resolved = memo.check_site(work, month, con)
            click.echo(f"memo check: {len(resolved)} number resolved ({', '.join(resolved)})")
    finally:
        con.close()
