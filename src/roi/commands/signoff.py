"""`roi signoff`: a person's attestation that the month's memo numbers were reviewed."""

from __future__ import annotations

import re
import subprocess
import sys
from datetime import date
from pathlib import Path

import click

from roi import memo, signoff
from roi.output import reporting
from roi.workdir import RoiError, WorkDir


def _interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _git_name() -> str | None:
    out = subprocess.run(["git", "config", "user.name"], capture_output=True, text=True, check=False)
    return out.stdout.strip() or None


@click.command("signoff")
@click.option("--month", required=True, help="Month to sign off, YYYY-MM.")
@click.option("--work-dir", "work_dir", type=click.Path(file_okay=False, path_type=Path), default=None,
              help="Directory holding warehouse/ and site/; overrides the global --work-dir.")
@click.option("--json", "as_json", is_flag=True, help="Print one JSON object instead of text.")
@click.pass_obj
def command(work: WorkDir, month: str, work_dir: Path | None, as_json: bool) -> None:
    """Record that you reviewed the memo of MONTH.

    Needs a person at a terminal. The attestation is the signed commit that adds
    signoffs/<month>.yaml, not this file: `roi memo --require-signoff` checks that commit.
    """
    if work_dir is not None:
        work = WorkDir(work_dir)
    with reporting("signoff", as_json) as report:
        if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
            raise RoiError(2, f"bad month {month!r}; expected YYYY-MM")
        if not _interactive():
            raise RoiError(2, "roi signoff needs a person at a terminal; it refuses a non-interactive run")
        con = memo.open_warehouse(work)
        try:
            if not (work.site / "index.html").exists():
                raise RoiError(2, f"no memo at {work.site}; run `roi memo --month {month}` first")
            checked = memo.check_site(work, month, con)
        finally:
            con.close()
        report.check("memo", True, f"{len(checked.ids)} numbers resolve to their source rows")
        click.echo(f"The memo for {month} has {len(checked.ids)} numbers and every one resolves to its source rows.")
        click.echo(f"Memo content hash: {signoff.memo_hash(work.site)}")
        name = click.prompt("Your name", default=_git_name() or "")
        if not click.confirm(f"Sign off the memo for {month} as {name}?"):
            raise RoiError(2, "not signed off")
        written = signoff.write(work, month, name, date.today().isoformat(), checked.ids)
        report.check("signoff", True, f"wrote {written}")
        report.artifact(written)
        click.echo(f"wrote {written}")
        click.echo(f"Commit it with your hardware key: git add {written} && git commit -S -m 'signoff: {month}'")
