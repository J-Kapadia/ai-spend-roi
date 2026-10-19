"""`roi dbt` runs a dbt subcommand against the published warehouse, with dbt's output under the work dir."""

import hashlib
import shutil
from pathlib import Path

import pytest
from click.testing import CliRunner

from conftest import connect
from roi.cli import main

REPO_DBT = Path(__file__).resolve().parents[1] / "dbt"


def content_hash(db) -> str:
    """SHA-256 over every table except prov_build, in table-name order, rows ordered by all columns."""
    con = connect(str(db), read_only=True)
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


def roi(work, *args):
    return CliRunner().invoke(main, ["--work-dir", str(work), *args])


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    work = tmp_path_factory.mktemp("dbt_command")
    out = roi(work, "build", "--toy")
    assert out.exit_code == 0, out.output
    return work


def test_dbt_test_passes_prints_the_summary_and_leaves_the_warehouse_and_the_repository_alone(built):
    before = content_hash(built / "warehouse" / "roi.duckdb")
    out = roi(built, "dbt", "test")
    assert out.exit_code == 0, out.output
    assert "Completed successfully" in out.output and "PASS=" in out.output
    assert content_hash(built / "warehouse" / "roi.duckdb") == before
    assert not (REPO_DBT / "target").exists() and not (REPO_DBT / "logs").exists()
    assert (built / "warehouse" / "dbt" / "target").is_dir()
    assert not (built / "warehouse" / "dbt" / "roi.duckdb").exists()


def test_the_work_dir_option_after_the_dbt_arguments_selects_the_warehouse(built):
    out = CliRunner().invoke(main, ["dbt", "test", "--work-dir", str(built)])
    assert out.exit_code == 0, out.output


def test_a_one_cent_edit_of_the_ledger_makes_dbt_test_exit_3(built, tmp_path):
    work = tmp_path / "edited"
    shutil.copytree(built, work, symlinks=True)
    con = connect(str(work / "warehouse" / "roi.duckdb"))
    try:
        con.execute(
            "UPDATE raw_gl_journal SET debit_cents = debit_cents + 1 "
            "WHERE entry_id = (SELECT min(entry_id) FROM raw_gl_journal WHERE debit_cents > 0 "
            "AND account_code = (SELECT value FROM roi_settings WHERE key = 'gl_account'))"
        )
    finally:
        con.close()
    # The tests read the mart tables, so the edit reaches them once the models are rebuilt.
    assert roi(work, "dbt", "run").exit_code == 0
    out = roi(work, "dbt", "test")
    assert out.exit_code == 3, out.output
    assert "FAIL" in out.output
    assert "dbt test failed" in out.output


def test_no_warehouse_exits_2(tmp_path):
    out = roi(tmp_path, "dbt", "test")
    assert out.exit_code == 2
    assert "no warehouse; run `roi build` first" in out.output


def test_no_arguments_exits_2(built):
    assert roi(built, "dbt").exit_code == 2
