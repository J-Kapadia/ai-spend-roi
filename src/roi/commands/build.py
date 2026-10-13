"""`roi build`: load the books, run dbt, estimate, write provenance, publish by file swap."""

from __future__ import annotations

import os

import click

from roi import steps
from roi.gen import toy
from roi.workdir import RoiError, WorkDir


@click.command("build")
@click.option("--toy", "use_toy", is_flag=True, help="Build the 3-team, 6-month toy company.")
@click.pass_obj
def command(work: WorkDir, use_toy: bool) -> None:
    """Build the warehouse from the books."""
    if not use_toy:
        raise RoiError(2, "only --toy is available until the Beta Co generator lands")
    toy.generate(work)

    work.warehouse.mkdir(parents=True, exist_ok=True)
    work.warehouse_next_db.unlink(missing_ok=True)
    ctx = steps.BuildContext(work=work, db=work.warehouse_next_db, through=toy.TOY_THROUGH)
    for step in steps.discover():
        click.echo(f"step {step.NAME}")
        step.run(ctx)
    os.replace(work.warehouse_next_db, work.warehouse_db)
    click.echo(f"built {work.warehouse_db}")
