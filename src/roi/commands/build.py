"""`roi build`: load the books, run dbt, estimate, write provenance, publish by file swap."""

from __future__ import annotations

import csv
import os
from pathlib import Path

import click

from roi import steps
from roi.gen import books, config, panel, toy
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


def _remove(path: Path) -> None:
    path.unlink(missing_ok=True)
    path.with_name(path.name + ".wal").unlink(missing_ok=True)


@click.command("build")
@click.option("--toy", "use_toy", is_flag=True, help="Build the 3-team, 6-month toy company.")
@click.option("--config", "config_path", type=click.Path(dir_okay=False, path_type=Path), default=None,
              help="Generate the books from this generator config first. Without it (and without --toy) "
                   "the books already under the work dir are loaded.")
@click.option("--work-dir", "work_dir", type=click.Path(file_okay=False, path_type=Path), default=None,
              help="Directory for data/, truth/ and warehouse/; overrides the global --work-dir.")
@click.pass_obj
def command(work: WorkDir, use_toy: bool, config_path: Path | None, work_dir: Path | None) -> None:
    """Build the warehouse from the books.

    The books are loaded into warehouse/roi.next.duckdb, dbt builds and tests the models
    against that file, and only when every step has passed is it swapped onto roi.duckdb
    with one os.replace. A failed or killed build leaves the published file as it was.
    """
    if work_dir is not None:
        work = WorkDir(work_dir)
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

    through = _through(work)
    work.warehouse.mkdir(parents=True, exist_ok=True)
    if use_toy or config_path is not None:
        load.write_settings(work, gl_account)
    _remove(work.warehouse_next_db)  # left behind by an interrupted build
    ctx = steps.BuildContext(work=work, db=work.warehouse_next_db, through=through)
    try:
        for step in steps.discover():
            click.echo(f"step {step.NAME}")
            step.run(ctx)
    except BaseException:
        _remove(work.warehouse_next_db)
        raise
    os.replace(work.warehouse_next_db, work.warehouse_db)
    click.echo(f"built {work.warehouse_db}")
