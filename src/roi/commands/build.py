"""`roi build`: load the books, run dbt, estimate, write provenance, publish by file swap."""

from __future__ import annotations

import csv
from pathlib import Path

import click

from roi import rollforward, steps
from roi.gen import books, config, manifest, panel, toy
from roi.output import reporting
from roi.steps import load
from roi.workdir import RoiError, WorkDir


def _through(work: WorkDir) -> str:
    """The last month of the books: the latest month in the output file."""
    path = work.books / "raw_output.csv"
    try:
        with path.open(newline="", encoding="utf-8") as f:
            months = {row["month"][:7] for row in csv.DictReader(f)}
    except OSError:
        raise RoiError(2, f"no books at {work.books}; run `roi gen` or pass --config") from None
    if not months:
        raise RoiError(2, f"{path} has no rows")
    return max(months)


def _replay_months(work: WorkDir, built: str, through: str) -> list[str]:
    """The incoming months to replay after the books, up to `through`; exit 2 if `through` has no data."""
    if through < built:
        raise RoiError(2, f"--through {through} is before the last month of the books, {built}")
    months = [m for m in rollforward.incoming_months(work) if built < m <= through]
    if through > built and (not months or months[-1] != through):
        raise RoiError(2, f"no incoming data for {through} in {work.data / 'incoming'}")
    return months


@click.command("build")
@click.option("--toy", "use_toy", is_flag=True, help="Build the 3-team, 6-month toy company.")
@click.option("--config", "config_path", type=click.Path(dir_okay=False, path_type=Path), default=None,
              help="Generate the books from this generator config first. Without it (and without --toy) "
                   "the books already under the work dir are loaded.")
@click.option("--through", "through_opt", default=None,
              help="Replay the incoming months up to this one after the books, as `roi rollforward` would, "
                   "one after the other against the file being built. Default: the last month of the books.")
@click.option("--work-dir", "work_dir", type=click.Path(file_okay=False, path_type=Path), default=None,
              help="Directory for data/, truth/ and warehouse/; overrides the global --work-dir.")
@click.option("--json", "as_json", is_flag=True, help="Print one JSON object instead of text.")
@click.pass_obj
def command(
    work: WorkDir, use_toy: bool, config_path: Path | None, through_opt: str | None, work_dir: Path | None,
    as_json: bool,
) -> None:
    """Build the warehouse from the books.

    The books are loaded into warehouse/roi.next.duckdb, dbt builds and tests the models
    against that file, and only when every step has passed is it swapped onto roi.duckdb
    with one os.replace. A failed or killed build leaves the published file as it was.

    With --through, the incoming months after the books are ingested in order into the same file, so
    `roi build --through 2026-04` rebuilds what `roi build` then `roi rollforward --month 2026-04` made.
    """
    if work_dir is not None:
        work = WorkDir(work_dir)
    with reporting("build", as_json) as report:
        if use_toy and config_path is not None:
            raise RoiError(2, "--toy and --config cannot be combined")

        gl_account = load.DEFAULT_GL_ACCOUNT
        if use_toy:
            toy.generate(work)
            gl_account = toy.GL_ACCOUNT
        elif config_path is not None:
            cfg = config.load(config_path)
            books.write(cfg, panel.simulate(cfg), work)
            gl_account = cfg.gl_account

        built = _through(work)
        manifest.check_books(work.data)
        report.check("manifest", True, "the books match data/MANIFEST.sha256")
        through = built if through_opt is None else through_opt
        rollforward.check_month(through)
        replay = _replay_months(work, built, through)
        work.warehouse.mkdir(parents=True, exist_ok=True)
        if use_toy or config_path is not None:
            load.write_settings(work, gl_account)
        with rollforward.locked(work):
            rollforward.promote_pending(work)
            rollforward.remove_db(work.warehouse_next_db)  # left behind by an interrupted build
            ctx = steps.BuildContext(work=work, db=work.warehouse_next_db, through=built)
            try:
                for step in steps.discover():
                    click.echo(f"step {step.NAME}")
                    step.run(ctx)
                    report.check(f"step:{step.NAME}", True, "passed")
                for month in replay:
                    rollforward.advance(work, work.warehouse_next_db, month)
                    report.check(f"replay:{month}", True, "ingested, dbt tests passed, spend ties to the ledger")
            except BaseException:
                rollforward.remove_db(work.warehouse_next_db)
                raise
            rollforward.publish(work, work.warehouse_next_db, keep_previous=False)
        click.echo(f"built {work.warehouse_db}")
        report.artifact(work.warehouse_db)
