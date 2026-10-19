"""`roi memo`: render the static site; with --check, verify every number resolves."""

from __future__ import annotations

import re
from pathlib import Path

import click

from roi import memo, signoff
from roi.output import reporting
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
@click.option("--json", "as_json", is_flag=True, help="Print one JSON object instead of text.")
@click.pass_obj
def command(
    work: WorkDir, month: str, check: bool, require_signoff: bool, work_dir: Path | None, as_json: bool
) -> None:
    """Build the memo site into site/."""
    if work_dir is not None:
        work = WorkDir(work_dir)
    problems: list[str] = []
    with reporting("memo", as_json) as report:
        if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
            raise RoiError(2, f"bad month {month!r}; expected YYYY-MM")
        con = memo.open_warehouse(work)
        try:
            index = memo.build_site(work, month, con)
            click.echo(f"wrote {index}")
            report.check("tie", True, "spend ties to the ledger to the cent")
            for page in sorted(work.site.rglob("*.html")):
                report.artifact(page)
            if check or require_signoff:
                checked = memo.check_site(work, month, con)
                click.echo(f"memo check: {_numbers(len(checked.ids))} resolved")
                click.echo(f"  pages: {len(checked.pages)} checked (the index and {len(checked.pages) - 1} team pages)")
                report.check("pages", True, f"{len(checked.pages)} pages checked")
                for section, n in sorted(checked.by_section.items()):
                    click.echo(f"  {section}: {_numbers(n)} resolved")
                    report.check(f"resolved:{section}", True, f"{_numbers(n)} resolved")
                if checked.lit:
                    click.echo(f"  static numbers for review: {', '.join(checked.lit)}")
                state = signoff.state(work, month)
                click.echo(f"sign-off: {state}")
                if require_signoff:
                    problems = signoff.verify(work, month)
                    report.check("signoff", not problems, "; ".join(problems) or "signed commit by a listed hardware key")
                    if not problems:
                        click.echo("sign-off: verified, signed commit by a listed hardware key")
                else:
                    report.check("signoff", True, state)
        finally:
            con.close()
    if problems:
        raise RoiError(1, "; ".join(problems))
