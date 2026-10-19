"""`roi rollforward`: ingest one month into a copy of the warehouse, check it, swap the file."""

from __future__ import annotations

from pathlib import Path

import click

from roi import rollforward
from roi.output import reporting
from roi.workdir import WorkDir


@click.command("rollforward")
@click.option("--month", required=True, help="The month to ingest, YYYY-MM: the one after the last in the warehouse.")
@click.option("--config", "config_path", type=click.Path(dir_okay=False, path_type=Path), default=None,
              help="Generator config, to find the roi config beside it (as for `roi build --config`). "
                   "Nothing is generated.")
@click.option("--work-dir", "work_dir", type=click.Path(file_okay=False, path_type=Path), default=None,
              help="Directory holding data/ and warehouse/; overrides the global --work-dir.")
@click.option("--json", "as_json", is_flag=True, help="Print one JSON object instead of text.")
@click.pass_obj
def command(work: WorkDir, month: str, config_path: Path | None, work_dir: Path | None, as_json: bool) -> None:
    """Add a month to the warehouse.

    The warehouse is copied to warehouse/roi.staging.duckdb, the files of data/incoming/<month>/ are
    checked against data/MANIFEST.incoming.sha256 and appended, dbt rebuilds and tests the models, the
    spend must tie to the ledger to the cent, and the results for the month are written beside the
    earlier months'. Only then is the staged file swapped onto roi.duckdb, keeping the old one as
    roi.prev.duckdb. A failed or killed run leaves roi.duckdb as it was. Run `roi verify --through
    <month>` afterwards for the statistical checks.

    A month already ingested with identical files is a no-op. Exit 2: month out of order, files missing,
    or an ingested month with different files. Exit 3: manifest, dbt test or ledger tie failure.
    """
    if work_dir is not None:
        work = WorkDir(work_dir)
    with reporting("rollforward", as_json) as report:
        rollforward.rollforward(work, month)
        # A failing step raises, so reaching here means the manifest, dbt tests and ledger tie passed
        # (or the month was already ingested, identical) and the staged file was swapped in.
        for check_id, detail in (
            ("manifest", "the incoming files match data/MANIFEST.incoming.sha256"),
            ("dbt", "models built and tests passed"),
            ("gl_tie", "spend ties to the ledger to the cent"),
            ("swap", f"{month} published"),
        ):
            report.check(check_id, True, detail)
        report.artifact(work.warehouse_db)
