"""Allocate the configured monthly budget over the measured teams into the file being built.

Writes `alloc_result` in one transaction: one row per measured team, with a zero amount for a
team whose curve is not identified or that is not funded. The budget is `memo_budget_usd` in
`config/roi.yaml` under the work dir; without the file or the key the table is written empty.
"""

from __future__ import annotations

import click
import duckdb

from roi import alloc
from roi import steps
from roi.steps import BuildContext

NAME = "alloc"
ORDER = 54

SCHEMA = (
    "CREATE TABLE alloc_result(through_month VARCHAR, budget_cents BIGINT, team_id VARCHAR, amount_cents BIGINT,"
    " capped BOOLEAN, funded_share_in_draws DOUBLE, beyond_observed_share DOUBLE)"
)


def rows(con: duckdb.DuckDBPyConnection, through: str, cfg: alloc.Config, budget_cents: int) -> list[tuple]:
    result = alloc.allocate(alloc.teams_from_warehouse(con, through, cfg), budget_cents)
    return [
        (through, budget_cents, r.team_id, r.amount_cents, r.capped, r.funded_share_in_draws, r.beyond_observed_share)
        for r in result.rows
    ]


def run(ctx: BuildContext) -> None:
    cfg = alloc.load_config(alloc.config_path(ctx.work))
    con = duckdb.connect(str(ctx.db))
    try:
        out: list[tuple] = []
        if cfg is None or cfg.memo_budget_usd is None:
            click.echo("  no memo_budget_usd in config/roi.yaml: no allocation written")
        else:
            out = rows(con, ctx.through, cfg, round(cfg.memo_budget_usd * 100))
        con.execute("BEGIN")
        steps.create_table(con, SCHEMA)
        if out:
            con.executemany("INSERT INTO alloc_result VALUES (?, ?, ?, ?, ?, ?, ?)", out)
        con.execute("COMMIT")
    finally:
        con.close()
