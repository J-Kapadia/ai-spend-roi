"""DuckDB connections, memory-capped.

DuckDB's default is 80% of the machine's memory per connection; a build, its dbt subprocess
and tests running in parallel each open one, so every connection the product opens takes
the cap in ROI_DUCKDB_MEMORY_LIMIT (default 2GB) and spills to disk beside the database file.
Likewise each connection would use every core, so each takes the thread cap in
ROI_DUCKDB_THREADS (default 2).
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import duckdb

MEMORY_LIMIT_VAR = "ROI_DUCKDB_MEMORY_LIMIT"
DEFAULT_MEMORY_LIMIT = "2GB"
# Likewise DuckDB would use every core per connection: several processes each running a
# query at once would make the load several times the core count.
THREADS_VAR = "ROI_DUCKDB_THREADS"
DEFAULT_THREADS = 2


def memory_limit() -> str:
    """The cap on every connection: `ROI_DUCKDB_MEMORY_LIMIT`, default 2GB."""
    return os.environ.get(MEMORY_LIMIT_VAR) or DEFAULT_MEMORY_LIMIT


def threads() -> int:
    """The thread cap on every connection: `ROI_DUCKDB_THREADS`, default 2."""
    raw = os.environ.get(THREADS_VAR) or str(DEFAULT_THREADS)
    if not raw.isdigit() or int(raw) < 1:
        raise ValueError(f"{THREADS_VAR} must be a positive integer, not {raw!r}")
    return int(raw)


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

    The caps and the spill directory are applied with SET after opening rather than passed as
    connection config: DuckDB shares one instance per file within a process and refuses a
    second connection opened with a different config."""
    if path is None:
        con = duckdb.connect(":memory:")
        where = None
    else:
        where = Path(path)
        con = duckdb.connect(str(where), read_only=read_only)
    con.execute(f"SET memory_limit = {_quoted(memory_limit())}")
    con.execute(f"SET threads = {threads()}")
    con.execute(f"SET temp_directory = {_quoted(str(temp_directory(where)))}")
    return con
