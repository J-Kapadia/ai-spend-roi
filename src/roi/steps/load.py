"""Load the books as raw_* tables into the file being built."""

from __future__ import annotations

import json


from roi.db import connect as db_connect
from roi.steps import BuildContext
from roi.workdir import RoiError, WorkDir

NAME = "load"
ORDER = 10

# Table schemas of the books.
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


# The months `roi rollforward` has ingested, with the SHA-256 of each file: written by the roll-forward only.
ROLLFORWARD_INGEST_DDL = "CREATE TABLE rollforward_ingest(month DATE, file VARCHAR, sha256 VARCHAR)"

DEFAULT_GL_ACCOUNT = "6100"
SETTINGS_FILE = "build_settings.json"


def write_settings(work: WorkDir, gl_account: str) -> None:
    """Record the build settings `roi build` was given, for the load step to put in the warehouse."""
    work.warehouse.mkdir(parents=True, exist_ok=True)
    (work.warehouse / SETTINGS_FILE).write_text(json.dumps({"gl_account": gl_account}) + "\n", encoding="utf-8")


def _settings(work: WorkDir) -> dict[str, str]:
    path = work.warehouse / SETTINGS_FILE
    settings = {"gl_account": DEFAULT_GL_ACCOUNT}
    if path.exists():
        settings.update(json.loads(path.read_text(encoding="utf-8")))
    return settings


def run(ctx: BuildContext) -> None:
    for table in TABLES:
        if not (ctx.work.books / f"{table}.csv").exists():
            raise RoiError(2, f"no books at {ctx.work.books}: missing {table}.csv; run `roi gen` or pass --config")
    con = db_connect(ctx.db)
    try:
        # The settings the models and the verification read back, such as the ledger account of AI usage.
        con.execute("CREATE TABLE roi_settings(key VARCHAR, value VARCHAR)")
        con.executemany("INSERT INTO roi_settings VALUES (?, ?)", sorted(_settings(ctx.work).items()))
        con.execute(ROLLFORWARD_INGEST_DDL)
        for table, columns in TABLES.items():
            path = ctx.work.books / f"{table}.csv"
            con.execute(
                f"CREATE TABLE {table} AS SELECT * FROM read_csv(?, header = true, columns = ?)",
                [str(path), columns],
            )
    finally:
        con.close()
