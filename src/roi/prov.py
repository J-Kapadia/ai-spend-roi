"""Provenance records: a named dbt model plus a stored SQL predicate,
with row count and checksum. `prov_id` is deterministic, so the memo is byte-identical
across rebuilds; build facts live in the separate, append-only `prov_build`.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import duckdb
import yaml

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
    cur = con.execute(f"SELECT * FROM {model} WHERE {predicate} ORDER BY ALL")
    return [d[0] for d in cur.description], cur.fetchall()


# An assumption number (a value printed from a config file) has no table: its source model is
# `config` and its predicate names the file and the key, as `roi.yaml: value_per_unit.support.base`.
CONFIG_MODEL = "config"


def config_predicate(file: str, key: str) -> str:
    return f"{file}: {key}"


def config_rows(predicate: str, config: Path | None) -> tuple[list[str], list[tuple]]:
    """The one (key, value) row a config predicate names; no rows when the file or key is absent."""
    if config is None or not config.exists():
        return ["key", "value"], []
    key = predicate.split(": ", 1)[1]
    node = yaml.safe_load(config.read_text(encoding="utf-8")) or {}
    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            return ["key", "value"], []
        node = node[part]
    if node is None or isinstance(node, (dict, list)):
        return ["key", "value"], []
    return ["key", "value"], [(key, float(node))]


def rows_of(
    con: duckdb.DuckDBPyConnection, model: str, predicate: str, config: Path | None = None
) -> tuple[list[str], list[tuple]]:
    if model == CONFIG_MODEL:
        return config_rows(predicate, config)
    return source_rows(con, model, predicate)


def derive(
    con: duckdb.DuckDBPyConnection,
    name: str,
    through_month: str,
    model: str,
    predicate: str,
    display_value: str,
    value: float,
    config: Path | None = None,
) -> Record:
    """A record computed from the current warehouse (or config) rather than read from `prov_record`.

    The id is the same function of the same fields as for a stored record, so a derived record
    and a stored one for the same number would carry the same id.
    """
    _, rows = rows_of(con, model, predicate, config)
    return Record(
        make_id(name, through_month, model, predicate, display_value), name, date.fromisoformat(f"{through_month}-01"),
        display_value, value, model, predicate, len(rows), checksum(rows), "memo",
    )


def checksum(rows: list[tuple]) -> str:
    text = "\n".join("|".join(_fmt(v) for v in row) for row in rows)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def recheck(con: duckdb.DuckDBPyConnection, rec: Record, config: Path | None = None) -> tuple[int, str]:
    """Re-run the stored predicate; return the row count and checksum it gives now."""
    _, rows = rows_of(con, rec.source_model, rec.source_predicate, config)
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
