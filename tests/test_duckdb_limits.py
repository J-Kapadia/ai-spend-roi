"""Every DuckDB connection the product opens is memory-capped, and dbt's connection too.

DuckDB's default is 80% of the machine's memory per connection; a build, its dbt subprocess
and tests running in parallel each open one, so every connection takes the cap in
ROI_DUCKDB_MEMORY_LIMIT (default 2GB) and spills beside the database file. Likewise each
connection would use every core, so each takes the thread cap in ROI_DUCKDB_THREADS
(default 2).
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import jinja2
import pytest
import yaml

from roi import db

VAR = "ROI_DUCKDB_MEMORY_LIMIT"
THREADS = "ROI_DUCKDB_THREADS"
DBT_DIR = Path(__file__).resolve().parents[1] / "dbt"
UNITS = {
    "B": 1, "bytes": 1,
    "KiB": 1024, "MiB": 1024**2, "GiB": 1024**3, "TiB": 1024**4,
    "KB": 1000, "MB": 1000**2, "GB": 1000**3, "TB": 1000**4,
}


def as_bytes(setting: str) -> float:
    """DuckDB reports a limit in binary units, rounded: `1.8 GiB`."""
    m = re.fullmatch(r"([0-9.]+)\s*([A-Za-z]+)", setting.strip())
    assert m, setting
    return float(m.group(1)) * UNITS[m.group(2)]


def settings(con) -> tuple[str, str]:
    return con.execute(
        "SELECT current_setting('memory_limit'), current_setting('temp_directory')"
    ).fetchone()


def test_file_connection_takes_the_default_cap_and_spills_beside_the_file(tmp_path, monkeypatch):
    monkeypatch.delenv(VAR, raising=False)
    con = db.connect(tmp_path / "w.duckdb")
    limit, temp = settings(con)
    con.close()
    assert abs(as_bytes(limit) - 2 * 1000**3) < 0.05 * 2 * 1000**3, limit
    assert Path(temp) == tmp_path / "w.duckdb.tmp"


def test_env_var_overrides_the_cap(tmp_path, monkeypatch):
    monkeypatch.setenv(VAR, "512MiB")
    con = db.connect(tmp_path / "w.duckdb")
    assert settings(con)[0] == "512.0 MiB"
    con.close()


def test_read_only_connection_is_capped(tmp_path, monkeypatch):
    monkeypatch.setenv(VAR, "512MiB")
    con = db.connect(tmp_path / "w.duckdb")
    con.execute("CREATE TABLE t (x INTEGER)")
    con.close()
    con = db.connect(tmp_path / "w.duckdb", read_only=True)
    limit, temp = settings(con)
    con.close()
    assert limit == "512.0 MiB"
    assert Path(temp) == tmp_path / "w.duckdb.tmp"


def test_in_memory_connection_is_capped(monkeypatch):
    monkeypatch.setenv(VAR, "512MiB")
    con = db.connect()
    limit, temp = settings(con)
    con.close()
    assert limit == "512.0 MiB"
    assert Path(temp).name == f"roi-duckdb-{os.getpid()}.tmp"


def test_dbt_profile_reads_the_same_cap(monkeypatch):
    """The profile `roi build` passes (`--profiles-dir dbt`) sets DuckDB's memory_limit from
    the same variable with the same default."""
    raw = yaml.safe_load((DBT_DIR / "profiles.yml").read_text())
    template = raw["roi"]["outputs"]["default"]["settings"]["memory_limit"]
    assert VAR in template

    def render() -> str:
        return jinja2.Template(template).render(env_var=lambda k, d=None: os.environ.get(k, d))

    monkeypatch.delenv(VAR, raising=False)
    assert render() == db.DEFAULT_MEMORY_LIMIT
    monkeypatch.setenv(VAR, "512MiB")
    assert render() == "512MiB"


def thread_count(con) -> int:
    return con.execute("SELECT current_setting('threads')").fetchone()[0]


def test_connections_take_the_default_thread_cap(tmp_path, monkeypatch):
    monkeypatch.delenv(THREADS, raising=False)
    con = db.connect(tmp_path / "w.duckdb")
    con.execute("CREATE TABLE t (x INTEGER)")
    assert thread_count(con) == db.DEFAULT_THREADS == 2
    con.close()
    con = db.connect(tmp_path / "w.duckdb", read_only=True)
    assert thread_count(con) == 2
    con.close()
    con = db.connect()
    assert thread_count(con) == 2
    con.close()


def test_env_var_overrides_the_thread_cap(tmp_path, monkeypatch):
    monkeypatch.setenv(THREADS, "3")
    con = db.connect(tmp_path / "w.duckdb")
    assert thread_count(con) == 3
    con.close()
    monkeypatch.setenv(THREADS, "zero")
    with pytest.raises(ValueError, match=THREADS):
        db.connect(tmp_path / "w.duckdb")


def test_dbt_profile_reads_the_same_thread_cap(monkeypatch):
    raw = yaml.safe_load((DBT_DIR / "profiles.yml").read_text())
    template = raw["roi"]["outputs"]["default"]["settings"]["threads"]
    assert THREADS in template

    def render() -> str:
        return jinja2.Template(template).render(env_var=lambda k, d=None: os.environ.get(k, d))

    monkeypatch.delenv(THREADS, raising=False)
    assert render() == str(db.DEFAULT_THREADS)
    monkeypatch.setenv(THREADS, "3")
    assert render() == "3"
