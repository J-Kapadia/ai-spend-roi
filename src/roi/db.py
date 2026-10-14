"""DuckDB connections, memory-capped.

DuckDB's default is 80% of the machine's memory per connection; a build, its dbt subprocess
and tests running in parallel each open one, so every connection the product opens takes
the cap in ROI_DUCKDB_MEMORY_LIMIT (default 2GB) and spills to disk beside the database file.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import duckdb

MEMORY_LIMIT_VAR = "ROI_DUCKDB_MEMORY_LIMIT"
DEFAULT_MEMORY_LIMIT = "2GB"


def memory_limit() -> str:
    """The cap on every connection: `ROI_DUCKDB_MEMORY_LIMIT`, default 2GB."""
    return os.environ.get(MEMORY_LIMIT_VAR) or DEFAULT_MEMORY_LIMIT


def temp_directory(path: Path | None) -> Path:
    """Where a connection spills: beside the database file, or for an in-memory database
    under the system temp directory, one per process."""
    if path is None:
        return Path(tempfile.gettempdir()) / f"roi-duckdb-{os.getpid()}.tmp"
    return path.parent / f"{path.name}.tmp"


def _quoted(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def connect(path: Path | str | None = None, *, read_only: bool = False) -> duckdb.DuckDBPyConnection:
    """Open the database at `path`, or an in-memory one when `path` is None, capped.

    The cap and the spill directory are applied with SET after opening rather than passed as
    connection config: DuckDB shares one instance per file within a process and refuses a
    second connection opened with a different config."""
    if path is None:
        con = duckdb.connect(":memory:")
        where = None
    else:
        where = Path(path)
        con = duckdb.connect(str(where), read_only=read_only)
    con.execute(f"SET memory_limit = {_quoted(memory_limit())}")
    con.execute(f"SET temp_directory = {_quoted(str(temp_directory(where)))}")
    return con
