"""`roi verify`: run the verification groups against the published warehouse."""

from __future__ import annotations

import re
from pathlib import Path

import click

from roi import estimator, rollforward, verify_books, verify_truth
from roi.workdir import RoiError, WorkDir


def _fmt(x: float | None) -> str:
    return "not estimable" if x is None else f"{x:+.4f}"


def _leave_one_out(rows: list[verify_books.ControlSensitivity]) -> list[str]:
    lines = ["leave-one-control-out (a report, not a check): each control dropped in turn"]
    if not rows:
        lines.append("  no measured teams")
    for r in rows:
        head = f"  {r.team_id}: {_fmt(r.effect)} [{r.ci_lo:+.4f}, {r.ci_hi:+.4f}]"
        if r.single_control:
            lines.append(f"{head}; one control ({', '.join(r.controls)}), not run")
            continue
        parts = []
        for run in r.runs:
            flag = ""
            if run.effect is not None and not r.ci_lo <= run.effect <= r.ci_hi:
                flag = " outside the interval"
            parts.append(f"without {run.dropped} {_fmt(run.effect)} ({run.cells} cells){flag}")
        lines.append(f"{head}; " + "; ".join(parts))
    return lines


@click.command("verify")
@click.option("--through", required=True, help="Last month to verify, YYYY-MM.")
@click.option("--work-dir", "work_dir", type=click.Path(file_okay=False, path_type=Path), default=None,
              help="Directory holding warehouse/ and truth/; overrides the global --work-dir.")
@click.pass_obj
def command(work: WorkDir, through: str, work_dir: Path | None) -> None:
    """Verify the warehouse: (a) spend ties to the ledger to the cent, (b) each measured team's
    planted effect lies inside its 95% interval, (c) unmeasured teams have no ROI, and the
    leave-one-control-out report."""
    if work_dir is not None:
        work = WorkDir(work_dir)
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", through):
        raise RoiError(2, f"bad month {through!r}; expected YYYY-MM")
    if work.warehouse_db.exists():
        rollforward.recover(work)  # finish a roll-forward that was killed between its file and marker swaps
    con = verify_books.open_warehouse(work)
    try:
        months = {m for (m,) in con.execute("SELECT DISTINCT strftime(month, '%Y-%m') FROM raw_output").fetchall()}
        if through not in months:
            raise RoiError(2, f"the books have no output for {through}")
        tie = verify_books.check_gl_tie(con, through)
        click.echo(f"verify through {through}")
        click.echo(
            f"(a) GL tie: ok, {tie.team_months} team-months and {tie.months} company-months tie to the cent,"
            " and the mart agrees"
        )
        built = verify_books.result_months(con)
        if through not in built:
            # Results are kept per through month, and only the built months have any.
            click.echo(f"(b) interval coverage: not run, the warehouse has results through {built[-1] if built else '-'} only")
            click.echo("(c) unmeasured discipline: not run, for the same reason")
            return
        coverage = verify_truth.check_interval_coverage(con, work, through)
        discipline = verify_books.check_unmeasured_discipline(
            con, through, empty_agenda_ok=not (work.root / "config" / "roi.yaml").exists()
        )
        degenerate = verify_books.allocation_degenerate(con, through)
        control_sensitivity = verify_books.leave_one_control_out(estimator.read_panel(con, through), through)
    finally:
        con.close()

    all_failures: list[str] = []
    failures = coverage.failures
    click.echo(
        f"(b) interval coverage: {'ok' if not failures else 'FAILED'}, planted effect inside the 95% interval for "
        f"{len(coverage.teams) - len(failures)} of {len(coverage.teams)} measured teams"
    )
    for c in coverage.teams:
        click.echo(
            f"    {c.team_id}: planted {c.planted:+.4f}, effect {c.effect:+.4f} [{c.ci_lo:+.4f}, {c.ci_hi:+.4f}]"
            f"{'' if c.inside else '  OUTSIDE'}"
        )
    all_failures += failures
    for line in failures:
        click.echo(f"    {line}")

    failures = list(discipline.failures)
    measured = discipline.with_status("measured")
    unmeasured = discipline.with_status("unmeasured")
    control = discipline.with_status("control")
    click.echo(f"(c) unmeasured discipline: {'ok' if not failures else 'FAILED'}")
    click.echo(f"    measured ({len(measured)}): {', '.join(t.team_id for t in measured) or '-'}")
    click.echo(
        f"    unmeasured ({len(unmeasured)}): "
        + (", ".join(f"{t.team_id} ({t.reason})" for t in unmeasured) or "-")
    )
    click.echo(f"    control ({len(control)}): {', '.join(t.team_id for t in control) or '-'}")
    click.echo(f"    checked: {', '.join(discipline.checked)}")
    if discipline.empty:
        click.echo(f"    written empty, no config/roi.yaml in the work dir: {', '.join(discipline.empty)}")
    if discipline.not_built:
        click.echo(f"    not built yet: {', '.join(discipline.not_built)}")
    all_failures += failures
    for line in failures:
        click.echo(f"    {line}")

    if degenerate:
        click.echo(f"warning: {degenerate}")

    for line in _leave_one_out(control_sensitivity):
        click.echo(line)
    if all_failures:
        raise RoiError(1, "\n".join(all_failures))
