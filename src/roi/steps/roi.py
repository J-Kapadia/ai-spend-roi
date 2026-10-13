"""ROI and its sensitivity for the measured teams, into the file being built.

Writes `roi_team` and `roi_sensitivity` in one transaction. A team that is not `measured`
gets no row. The value per unit comes from the roi config; a measured team that the config
lists without values stops the build with exit 4 (the author enters them at gate G1), and a
measured team the config does not list gets no row (the config describes one company).
"""

from __future__ import annotations

from pathlib import Path

import click
import duckdb

from roi import roi as roi_calc
from roi.steps import BuildContext
from roi.workdir import RoiError, WorkDir

NAME = "roi"
ORDER = 35

DEFAULT_CONFIG = Path(__file__).resolve().parents[3] / "config" / "roi.yaml"

SCHEMA = (
    "CREATE TABLE roi_team(through_month VARCHAR, team_id VARCHAR, benefit_cents BIGINT, spend_cents BIGINT,"
    " roi_multiple DOUBLE, roi_lo DOUBLE, roi_hi DOUBLE, breakeven_value_cents BIGINT, baseline_outcome DOUBLE,"
    " roi_verdict VARCHAR)",
    "CREATE TABLE roi_sensitivity(through_month VARCHAR, team_id VARCHAR, value_multiplier DOUBLE,"
    " roi_multiple DOUBLE, roi_survives_share DOUBLE)",
)


def sibling(gen_config: Path) -> Path | None:
    """The roi config that goes with a generator config: `gen.yaml` -> `roi.yaml`, `gen_x.yaml` -> `roi_x.yaml`."""
    stem = gen_config.stem
    if stem == "gen" or stem.startswith("gen_"):
        return gen_config.with_name("roi" + stem[3:] + gen_config.suffix)
    return None


def _generator_config() -> Path | None:
    """The `--config` the running `roi build` was given, if any."""
    ctx = click.get_current_context(silent=True)
    while ctx is not None:
        path = ctx.params.get("config_path")
        if path is not None:
            return Path(path)
        ctx = ctx.parent
    return None


def config_path(work: WorkDir) -> Path:
    """The roi config of this build: `<work-dir>/config/roi.yaml`, else the one beside the
    generator config the build was given, else the repository's `config/roi.yaml`."""
    local = work.root / "config" / "roi.yaml"
    if local.exists():
        return local
    gen = _generator_config()
    beside = sibling(gen) if gen is not None else None
    if beside is not None and beside.exists():
        return beside
    return DEFAULT_CONFIG


def rows(through: str, inputs: list[roi_calc.TeamInput], cfg: roi_calc.RoiConfig) -> tuple[list, list, list[str]]:
    """The two result tables' rows, and the measured teams the config does not list."""
    unlisted = [i.team_id for i in inputs if i.team_id not in cfg.teams]
    listed = [i for i in inputs if i.team_id in cfg.teams]
    for i in listed:
        if cfg.teams[i.team_id].kind not in (None, i.kind):
            raise RoiError(
                2, f"the roi config has kind {cfg.teams[i.team_id].kind} for {i.team_id}, the books say {i.kind}"
            )
    missing = [i.team_id for i in listed if cfg.teams[i.team_id].value is None]
    if missing:
        raise RoiError(
            4, f"no value per unit for {', '.join(missing)} in {cfg.path}: the author enters them at gate G1"
        )
    team_rows, sens_rows = [], []
    for i in listed:
        r = roi_calc.compute_team(i, cfg.teams[i.team_id].value, cfg)
        team_rows.append((
            through, r.team_id, r.benefit_cents, r.spend_cents, r.roi_multiple, r.roi_lo, r.roi_hi,
            r.breakeven_value_cents, r.baseline_outcome, r.verdict,
        ))
        sens_rows.extend((through, r.team_id, m, roi, r.roi_survives_share) for m, roi in r.grid)
    return team_rows, sens_rows, unlisted


def run(ctx: BuildContext) -> None:
    cfg = roi_calc.load_config(config_path(ctx.work))
    con = duckdb.connect(str(ctx.db))
    try:
        team_rows, sens_rows, unlisted = rows(ctx.through, roi_calc.read_inputs(con, ctx.through), cfg)
        con.execute("BEGIN")
        for statement in SCHEMA:
            con.execute(statement)
        if team_rows:  # executemany refuses an empty list
            con.executemany("INSERT INTO roi_team VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", team_rows)
            con.executemany("INSERT INTO roi_sensitivity VALUES (?, ?, ?, ?, ?)", sens_rows)
        con.execute("COMMIT")
    finally:
        con.close()
    if unlisted:
        click.echo(f"no ROI for {', '.join(unlisted)}: not in {cfg.path}")
