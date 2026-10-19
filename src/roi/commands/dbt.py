"""`roi dbt`: run any dbt subcommand against the published warehouse, with the profile and output
placement `roi build` uses."""

from __future__ import annotations

from pathlib import Path

import click

from roi import rollforward
from roi.steps.dbt import run_dbt
from roi.workdir import RoiError, WorkDir


@click.command("dbt", context_settings={"ignore_unknown_options": True})
@click.argument("args", nargs=-1, required=True, type=click.UNPROCESSED)
@click.option("--work-dir", "work_dir", type=click.Path(file_okay=False, path_type=Path), default=None,
              help="Directory holding warehouse/; overrides the global --work-dir.")
@click.pass_obj
def command(work: WorkDir, args: tuple[str, ...], work_dir: Path | None) -> None:
    """Run `dbt ARGS...` against warehouse/roi.duckdb, for example `roi dbt test`.

    The target and log directories go under warehouse/dbt/, so nothing is written into the
    repository's dbt/ directory. Exit 2: no warehouse, or no dbt arguments. Exit 3: dbt failed.
    """
    if work_dir is not None:
        work = WorkDir(work_dir)
    if not work.warehouse_db.exists():
        raise RoiError(2, "no warehouse; run `roi build` first")
    subcommand = next((a for a in args if not a.startswith("-")), args[0])
    with rollforward.locked(work):
        proc = run_dbt(work.warehouse_db, work.warehouse / "dbt", list(args))
    click.echo(proc.stdout, nl=not proc.stdout.endswith("\n"))
    if proc.returncode != 0:
        raise RoiError(3, f"dbt {subcommand} failed")
