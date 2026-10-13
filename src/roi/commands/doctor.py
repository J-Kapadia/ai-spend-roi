"""`roi doctor`: the health of a tree (`--tree`) or of a release (`--release`)."""

from __future__ import annotations

import json
from pathlib import Path

import click

from roi import doctor
from roi.workdir import RoiError, WorkDir


@click.command("doctor")
@click.option("--tree", "mode", flag_value="tree", default=True, help="Checks that need only an extraction (the default).")
@click.option("--release", "mode", flag_value="release", help="Also the checks that need .git. Exits 4 without it.")
@click.option("--json", "as_json", is_flag=True, help="Print JSON instead of text.")
@click.option("--work-dir", "work_dir", type=click.Path(file_okay=False, path_type=Path), default=None,
              help="Directory holding data/ and warehouse/; overrides the global --work-dir.")
@click.pass_obj
def command(work: WorkDir, mode: str, as_json: bool, work_dir: Path | None) -> None:
    """Check the health of the tree: lock file, books, warehouse, dbt tests, ledger tie, truth boundary."""
    if work_dir is not None:
        work = WorkDir(work_dir)
    checks = doctor.release(work) if mode == "release" else doctor.tree(work)
    failed = [c for c in checks if not c.passed]
    if as_json:
        click.echo(json.dumps({
            "command": "doctor", "status": "fail" if failed else "pass",
            "checks": [{"id": c.id, "pass": c.passed, "detail": f"{c.status}: {c.detail}"} for c in checks],
            "artifacts": [],
        }, indent=2))
    else:
        for c in checks:
            click.echo(f"{c.status:<5} {c.id}: {c.detail}")
        click.echo(f"doctor --{mode}: {'FAILED, ' + str(len(failed)) + ' of ' + str(len(checks)) + ' checks' if failed else 'ok'}")
    if failed and as_json:
        raise SystemExit(1)
    if failed:
        raise RoiError(1, f"{len(failed)} check(s) failed: {', '.join(c.id for c in failed)}")
