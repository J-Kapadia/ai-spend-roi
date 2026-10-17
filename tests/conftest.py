"""Socket guard (no test may open a network connection) and DuckDB caps (every connection a
test opens carries the warehouse memory and thread limits)."""

import socket

import duckdb
import pytest

from roi import db as roi_db


@pytest.fixture(autouse=True)
def _socket_guard(monkeypatch):
    def refuse(self, *args, **kwargs):
        raise RuntimeError("network access is forbidden in tests (socket guard)")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)


@pytest.fixture(autouse=True, scope="session")
def _duckdb_caps():
    """Every DuckDB connection a test opens carries the warehouse caps (`roi.db`'s
    memory_limit and threads), so parallel test workers do not each size a connection to
    the whole machine. The caps are applied with SET after opening, as `roi.db.connect`
    does: DuckDB refuses a second connection to the same file with a different config."""
    real_connect = duckdb.connect

    def capped_connect(*args, **kwargs):
        con = real_connect(*args, **kwargs)
        con.execute(f"SET memory_limit = {roi_db._quoted(roi_db.memory_limit())}")
        con.execute(f"SET threads = {roi_db.threads()}")
        return con

    duckdb.connect = capped_connect
    try:
        yield
    finally:
        duckdb.connect = real_connect


def connect(*args, **kwargs):
    """`duckdb.connect` for tests: same arguments and return value, capped by the
    session fixture above."""
    return duckdb.connect(*args, **kwargs)
