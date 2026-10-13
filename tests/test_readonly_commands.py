"""`estimate`, `allocate` and `agenda` open the published warehouse read-only: the content hash
of the file is the same after each run."""

import hashlib

import duckdb
import pytest
from click.testing import CliRunner

from roi.cli import main

ROI_YAML = """
memo_budget_usd: 1000
value_per_unit:
  alpha: {low: 5.0, base: 10.0, high: 15.0}
  beta: {low: 5.0, base: 10.0, high: 15.0}
  gamma: {low: 5.0, base: 10.0, high: 15.0}
"""


def content_hash(db) -> str:
    """SHA-256 over every table except prov_build, in table-name order, rows ordered by all columns."""
    con = duckdb.connect(str(db), read_only=True)
    try:
        h = hashlib.sha256()
        tables = [t for (t,) in con.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main' ORDER BY table_name"
        ).fetchall() if t != "prov_build"]
        for table in tables:
            h.update(f"table {table}\n".encode())
            for row in con.execute(f"SELECT * FROM {table} ORDER BY ALL").fetchall():
                h.update(("|".join(repr(v) for v in row) + "\n").encode())
        return h.hexdigest()
    finally:
        con.close()


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    work = tmp_path_factory.mktemp("readonly")
    (work / "config").mkdir()
    (work / "config" / "roi.yaml").write_text(ROI_YAML)
    out = CliRunner().invoke(main, ["--work-dir", str(work), "build", "--toy"])
    assert out.exit_code == 0, out.output
    return work


@pytest.mark.parametrize("args", [
    ["estimate"],
    ["estimate", "--json"],
    ["allocate", "--budget", "1000"],
    ["allocate", "--budget", "1000", "--json"],
    ["agenda"],
    ["agenda", "--json"],
])
def test_the_command_leaves_the_content_hash_unchanged(built, args):
    db = built / "warehouse" / "roi.duckdb"
    before, file_before = content_hash(db), hashlib.sha256(db.read_bytes()).hexdigest()
    out = CliRunner().invoke(main, ["--work-dir", str(built), *args])
    assert out.exit_code == 0, out.output
    assert content_hash(db) == before
    assert hashlib.sha256(db.read_bytes()).hexdigest() == file_before
    assert not list((built / "warehouse").glob("*.wal"))


def test_the_commands_do_not_need_a_write_lock(built):
    """A reader holding the file open does not stop them, and they work on a read-only file."""
    db = built / "warehouse" / "roi.duckdb"
    reader = duckdb.connect(str(db), read_only=True)
    try:
        for args in (["estimate"], ["allocate", "--budget", "1000"], ["agenda"]):
            out = CliRunner().invoke(main, ["--work-dir", str(built), *args])
            assert out.exit_code == 0, out.output
    finally:
        reader.close()
    db.chmod(0o444)
    try:
        for args in (["estimate"], ["allocate", "--budget", "1000"], ["agenda"]):
            assert CliRunner().invoke(main, ["--work-dir", str(built), *args]).exit_code == 0
    finally:
        db.chmod(0o644)


def test_the_fixture_checks_do_not_open_the_warehouse(tmp_path):
    runner = CliRunner()
    assert runner.invoke(main, ["--work-dir", str(tmp_path), "allocate", "--budget", "50000", "--check"]).exit_code == 0
    assert runner.invoke(main, ["--work-dir", str(tmp_path), "agenda", "--check"]).exit_code == 0
    assert not (tmp_path / "warehouse").exists()


def test_the_commands_write_nothing_else_under_the_work_dir(built):
    before = sorted(p.relative_to(built) for p in built.rglob("*"))
    for args in (["estimate"], ["allocate", "--budget", "1000"], ["agenda"]):
        assert CliRunner().invoke(main, ["--work-dir", str(built), *args]).exit_code == 0
    assert sorted(p.relative_to(built) for p in built.rglob("*")) == before
