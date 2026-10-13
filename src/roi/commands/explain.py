"""`roi explain`: the provenance record behind one number of the memo, re-checked."""

from __future__ import annotations

import json
from pathlib import Path

import click

from roi import memo, prov, signoff
from roi.prov import Record
from roi.workdir import RoiError, WorkDir

MIN_PREFIX = 4


def _months(con) -> list[str]:
    months = {m for (m,) in con.execute("SELECT DISTINCT strftime(through_month, '%Y-%m') FROM prov_record").fetchall()}
    return sorted(months)


def find(work: WorkDir, con, wanted: str) -> tuple[Record, bool, str]:
    """The record whose id is, or starts with, `wanted`; whether it is stored; the month of the memo it is in."""
    wanted = wanted.strip().removesuffix(".html").rsplit("/", 1)[-1]
    if len(wanted) < MIN_PREFIX:
        raise RoiError(2, f"{wanted!r} is too short for a number id; copy it from the data-prov attribute")
    found: dict[str, tuple[Record, bool, str]] = {}
    stored = {pid for (pid,) in con.execute("SELECT prov_id FROM prov_record").fetchall()}
    for month in _months(con):
        _, collected = memo.collect(work, month, con)
        for rec in collected.records.values():
            if rec.prov_id.startswith(wanted):
                found.setdefault(rec.prov_id, (rec, rec.prov_id in stored, month))
    if not found:
        raise RoiError(2, f"no provenance record starts with {wanted}")
    if len(found) > 1:
        raise RoiError(2, f"{wanted} matches {len(found)} records: {', '.join(sorted(found))}")
    return next(iter(found.values()))


@click.command("explain")
@click.argument("number_id")
@click.option("--json", "as_json", is_flag=True, help="Print JSON instead of text.")
@click.option("--work-dir", "work_dir", type=click.Path(file_okay=False, path_type=Path), default=None,
              help="Directory holding warehouse/; overrides the global --work-dir.")
@click.pass_obj
def command(work: WorkDir, number_id: str, as_json: bool, work_dir: Path | None) -> None:
    """Print the provenance record of a number (the id in its data-prov attribute) and re-run its rows."""
    if work_dir is not None:
        work = WorkDir(work_dir)
    con = memo.open_warehouse(work)
    try:
        rec, is_stored, month = find(work, con, number_id)
        config = memo.roi_config_path(work)
        rows, digest = prov.recheck(con, rec, config)
        builds = con.execute(
            "SELECT build_id, git_sha, producer_kind, producer_id, checks_passed, created_at FROM prov_build"
            " WHERE prov_id = ? ORDER BY created_at, build_id", [rec.prov_id],
        ).fetchall()
    finally:
        con.close()
    ok = (rows, digest) == (rec.source_rows, rec.source_checksum)
    sign = signoff.load(work, month)
    cited = sign is not None and rec.prov_id in (sign.get("prov_ids") or [])
    if as_json:
        click.echo(json.dumps({
            "command": "explain", "status": "pass" if ok else "fail",
            "checks": [{"id": "recheck", "pass": ok, "detail": f"{rows} rows now, {rec.source_rows} stored"}],
            "artifacts": [],
            "record": {
                "prov_id": rec.prov_id, "name": rec.name, "through_month": month, "display_value": rec.display_value,
                "step": rec.step, "source_model": rec.source_model, "source_predicate": rec.source_predicate,
                "source_rows": rec.source_rows, "source_checksum": rec.source_checksum, "stored": is_stored,
                "builds": [dict(zip(("build_id", "git_sha", "producer_kind", "producer_id", "checks_passed",
                                     "created_at"), (str(v) for v in b))) for b in builds],
                "signoff": sign.get("name") if cited else None,
            },
        }, indent=2))
    else:
        click.echo(f"{rec.display_value}  {rec.name}  ({rec.prov_id})")
        click.echo(f"  through month: {month}")
        click.echo(f"  step: {rec.step}")
        click.echo(f"  source: {rec.source_model} where {rec.source_predicate}")
        click.echo(f"  rows: {rec.source_rows}, checksum {rec.source_checksum}")
        for b in builds:
            click.echo(f"  build {b[0]}: git {b[1]}, {b[2]} {b[3]}, checks {b[4]}, at {b[5]}")
        if not builds:
            click.echo("  build: none stored; this figure is derived from the result tables when the memo is built")
        click.echo(f"  sign-off: {'signoffs/%s.yaml, %s on %s' % (month, sign.get('name'), sign.get('date')) if cited else 'none'}")
        click.echo(f"  re-check: {'ok' if ok else 'FAILED'}, the predicate gives {rows} rows now")
    if not ok:
        raise RoiError(1, f"checksum mismatch for {rec.prov_id}: stored {rec.source_rows} rows, now {rows}")
