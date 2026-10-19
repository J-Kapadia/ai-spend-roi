"""Run `dbt build` (models and tests) against the file being built, as a subprocess."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from roi.steps import BuildContext
from roi.workdir import RoiError

NAME = "dbt"
ORDER = 20

DBT_DIR = Path(__file__).resolve().parents[3] / "dbt"


def run_dbt(db: Path, scratch: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    """Run `dbt <args>` against the DuckDB file `db`, with dbt's target and log directories under `scratch`.

    Raises RoiError(3) when dbt leaves a write-ahead log beside the link after a successful run.
    """
    scratch.mkdir(parents=True, exist_ok=True)
    # DuckDB names the catalog of "roi.next.duckdb" `roi`, but dbt-duckdb derives `roi.next`
    # from the same path and the two disagree. A symlink with a dot-free stem gives both `roi`.
    link = scratch / "roi.duckdb"
    link.unlink(missing_ok=True)
    link.symlink_to(db.resolve())
    cmd = [
        str(Path(sys.executable).parent / "dbt"), *args,
        "--project-dir", str(DBT_DIR), "--profiles-dir", str(DBT_DIR),
        "--target-path", str(scratch / "target"), "--log-path", str(scratch / "logs"),
        "--vars", json.dumps({"db_path": str(link)}),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    finally:
        link.unlink(missing_ok=True)
    # A write-ahead log left beside the link would be orphaned from the file being built.
    if proc.returncode == 0 and (scratch / "roi.duckdb.wal").exists():
        raise RoiError(3, "dbt left a write-ahead log beside the warehouse link")
    return proc


def run(ctx: BuildContext) -> None:
    proc = run_dbt(ctx.db, ctx.work.warehouse / "dbt", ["build"])
    if proc.returncode != 0:
        sys.stderr.write(proc.stdout)
        raise RoiError(3, "dbt build failed")
