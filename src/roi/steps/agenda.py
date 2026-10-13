"""Design an experiment for each unmeasured team into the file being built.

Writes `agenda_item` in one transaction: one row per unmeasured team and none for a measured
or control team. The value per unit comes from `config/roi.yaml` under the work dir; without
the file the table is written empty.
"""

from __future__ import annotations

import click
import duckdb

from roi import agenda, alloc
from roi import steps
from roi.steps import BuildContext

NAME = "agenda"
ORDER = 56

SCHEMA = (
    "CREATE TABLE agenda_item(through_month VARCHAR, team_id VARCHAR, design VARCHAR, n_per_arm INTEGER,"
    " length_months INTEGER, delta DOUBLE, sigma DOUBLE, rho DOUBLE, alpha DOUBLE, power DOUBLE, note VARCHAR,"
    " baseline_basis VARCHAR)"
)


def rows(con: duckdb.DuckDBPyConnection, through: str, cfg: alloc.Config) -> list[tuple]:
    return [
        (through, i.team_id, i.design, i.n_per_arm, i.length_months, i.delta, i.sigma, i.rho, i.alpha, i.power,
         i.note, i.baseline_basis)
        for i in agenda.items_from_warehouse(con, through, cfg)
    ]


def run(ctx: BuildContext) -> None:
    cfg = alloc.load_config(alloc.config_path(ctx.work))
    con = duckdb.connect(str(ctx.db))
    try:
        out: list[tuple] = []
        if cfg is None:
            click.echo("  no config/roi.yaml: no agenda written")
        else:
            out = rows(con, ctx.through, cfg)
        con.execute("BEGIN")
        steps.create_table(con, SCHEMA)
        if out:
            con.executemany("INSERT INTO agenda_item VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", out)
        con.execute("COMMIT")
    finally:
        con.close()
