"""Load the books as raw_* tables into the file being built (plan §6.2 step 2)."""

from __future__ import annotations

import duckdb

from roi.steps import BuildContext

NAME = "load"
ORDER = 10

# Table schemas from plan §7.1.
TABLES = {
    "raw_ai_spend": {
        "usage_date": "DATE", "team_id": "VARCHAR", "project_id": "VARCHAR", "user_id": "VARCHAR",
        "provider": "VARCHAR", "model": "VARCHAR", "input_tokens": "BIGINT", "output_tokens": "BIGINT",
        "cost_cents": "BIGINT",
    },
    "raw_gl_journal": {
        "entry_id": "VARCHAR", "period": "VARCHAR", "account_code": "VARCHAR", "cost_center": "VARCHAR",
        "debit_cents": "BIGINT", "credit_cents": "BIGINT", "memo": "VARCHAR",
    },
    "raw_hr_roster": {
        "employee_id": "VARCHAR", "team_id": "VARCHAR", "start_date": "DATE", "end_date": "DATE",
        "monthly_payroll_cents": "BIGINT",
    },
    "raw_output": {"team_id": "VARCHAR", "month": "DATE", "metric_id": "VARCHAR", "value": "DOUBLE"},
    "dim_team": {
        "team_id": "VARCHAR", "name": "VARCHAR", "metric_id": "VARCHAR", "unit_label": "VARCHAR",
        "direction": "TINYINT", "kind": "VARCHAR",
    },
}


def run(ctx: BuildContext) -> None:
    con = duckdb.connect(str(ctx.db))
    try:
        for table, columns in TABLES.items():
            path = ctx.work.books / f"{table}.csv"
            con.execute(
                f"CREATE TABLE {table} AS SELECT * FROM read_csv(?, header = true, columns = ?)",
                [str(path), columns],
            )
    finally:
        con.close()
