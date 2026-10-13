"""`roi memo`: render the static site; with --check, verify every number resolves."""

from __future__ import annotations

import re
from pathlib import Path

import click

from roi import memo, signoff
from roi.workdir import RoiError, WorkDir


def _numbers(n: int) -> str:
    return f"{n} number" if n == 1 else f"{n} numbers"


@click.command("memo")
@click.option("--month", required=True, help="Month to report, YYYY-MM.")
@click.option("--check", is_flag=True, help="Re-run every provenance predicate and compare.")
@click.option("--require-signoff", is_flag=True,
              help="Also require a sign-off for the month in a commit signed by a hardware key in "
                   "config/allowed-signers. Needs .git; the release job uses it. Implies --check.")
@click.option("--work-dir", "work_dir", type=click.Path(file_okay=False, path_type=Path), default=None,
              help="Directory holding warehouse/ and site/; overrides the global --work-dir.")
@click.pass_obj
def command(work: WorkDir, month: str, check: bool, require_signoff: bool, work_dir: Path | None) -> None:
    """Build the memo site into site/."""
    if work_dir is not None:
        work = WorkDir(work_dir)
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        raise RoiError(2, f"bad month {month!r}; expected YYYY-MM")
    con = memo.open_warehouse(work)
    try:
        index = memo.build_site(work, month, con)
        click.echo(f"wrote {index}")
        if check or require_signoff:
            checked = memo.check_site(work, month, con)
            click.echo(f"memo check: {_numbers(len(checked.ids))} resolved")
            for section, n in sorted(checked.by_section.items()):
                click.echo(f"  {section}: {_numbers(n)} resolved")
            if checked.lit:
                click.echo(f"  static numbers for review: {', '.join(checked.lit)}")
            state = signoff.state(work, month)
            click.echo(f"sign-off: {state}")
            if require_signoff:
                problems = signoff.verify(work, month)
                if problems:
                    raise RoiError(1, "; ".join(problems))
                click.echo("sign-off: verified, signed commit by a listed hardware key")
    finally:
        con.close()
