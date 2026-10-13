"""Provenance records (plan D-17): a named dbt model plus a stored SQL predicate,
with row count and checksum. `prov_id` is deterministic, so the memo is byte-identical
across rebuilds; build facts live in the separate, append-only `prov_build`.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date, datetime

import duckdb

PROV_RECORD_DDL = (
    "CREATE TABLE prov_record(prov_id VARCHAR PRIMARY KEY, name VARCHAR, through_month DATE,"
    " display_value VARCHAR, value DOUBLE, source_model VARCHAR, source_predicate VARCHAR,"
    " source_rows INTEGER, source_checksum VARCHAR, step VARCHAR)"
)
PROV_BUILD_DDL = (
    "CREATE TABLE prov_build(prov_id VARCHAR, build_id VARCHAR, git_sha VARCHAR, producer_kind VARCHAR,"
    " producer_id VARCHAR, checks_passed VARCHAR, created_at TIMESTAMP)"
)


@dataclass(frozen=True)
class Record:
    prov_id: str
    name: str
    through_month: date
    display_value: str
    value: float
    source_model: str
    source_predicate: str
    source_rows: int
    source_checksum: str
    step: str


COLUMNS = "prov_id, name, through_month, display_value, value, source_model, source_predicate, source_rows, source_checksum, step"


def make_id(name: str, through_month: str, source_model: str, source_predicate: str, display_value: str) -> str:
    key = "\x1f".join([name, through_month, source_model, source_predicate, display_value])
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def _fmt(v: object) -> str:
    if v is None:
        return ""
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, float):
        return repr(v)
    return str(v)


def source_rows(con: duckdb.DuckDBPyConnection, model: str, predicate: str) -> tuple[list[str], list[tuple]]:
    cur = con.execute(f"SELECT * FROM {model} WHERE {predicate} ORDER BY team_id, month")
    return [d[0] for d in cur.description], cur.fetchall()


def checksum(rows: list[tuple]) -> str:
    text = "\n".join("|".join(_fmt(v) for v in row) for row in rows)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def recheck(con: duckdb.DuckDBPyConnection, rec: Record) -> tuple[int, str]:
    """Re-run the stored predicate; return the row count and checksum it gives now."""
    _, rows = source_rows(con, rec.source_model, rec.source_predicate)
    return len(rows), checksum(rows)


def fetch(con: duckdb.DuckDBPyConnection, prov_id: str) -> Record | None:
    row = con.execute(f"SELECT {COLUMNS} FROM prov_record WHERE prov_id = ?", [prov_id]).fetchone()
    return Record(*row) if row else None


def fetch_named(con: duckdb.DuckDBPyConnection, name: str, through_month: str) -> Record | None:
    row = con.execute(
        f"SELECT {COLUMNS} FROM prov_record WHERE name = ? AND strftime(through_month, '%Y-%m') = ?",
        [name, through_month],
    ).fetchone()
    return Record(*row) if row else None
