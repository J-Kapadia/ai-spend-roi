"""A build killed between the load and the file swap leaves the published file alone,
and the next build resumes to the same warehouse as an uninterrupted one."""

import csv
import hashlib
import signal
import subprocess
import sys
from pathlib import Path

import duckdb
import pytest
from click.testing import CliRunner

from roi.cli import main

pytestmark = pytest.mark.slow

FIXTURE = Path(__file__).parent / "fixtures" / "gen_fixture.yaml"

# Runs `roi build` with the dbt step replaced by a SIGKILL of the process itself: the books are
# loaded into warehouse/roi.next.duckdb and nothing has been swapped yet.
KILL_AFTER_LOAD = """
import os, signal, sys
import roi.steps.dbt as dbt_step
dbt_step.run = lambda ctx: os.kill(os.getpid(), signal.SIGKILL)
from roi.cli import main
main(sys.argv[1:])
"""


def roi(work, *args):
    return CliRunner().invoke(main, ["--work-dir", str(work), *args])


def killed_build(work) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", KILL_AFTER_LOAD, "--work-dir", str(work), "build", "--config", str(FIXTURE)],
        capture_output=True, text=True, check=False,
    )


def published(work) -> Path:
    return work / "warehouse" / "roi.duckdb"


def leftover(work) -> Path:
    return work / "warehouse" / "roi.next.duckdb"


def file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fingerprint(path: Path) -> str:
    """Digest of every table's rows except the build facts, which carry a clock and a random id.
    A DuckDB file is not byte-reproducible, so two warehouses are compared by content."""
    con = duckdb.connect(str(path), read_only=True)
    try:
        h = hashlib.sha256()
        tables = [t for (t,) in con.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_type = 'BASE TABLE'"
            " AND table_name <> 'prov_build' ORDER BY table_name").fetchall()]
        for table in tables:
            h.update(table.encode())
            for row in con.execute(f"SELECT * FROM {table} ORDER BY ALL").fetchall():
                h.update(repr(row).encode())
        return h.hexdigest()
    finally:
        con.close()


def table_names(path: Path) -> set[str]:
    con = duckdb.connect(str(path), read_only=True)
    try:
        return {t for (t,) in con.execute("SELECT table_name FROM information_schema.tables").fetchall()}
    finally:
        con.close()


@pytest.fixture(scope="module")
def uninterrupted(tmp_path_factory):
    work = tmp_path_factory.mktemp("uninterrupted")
    result = roi(work, "build", "--config", str(FIXTURE))
    assert result.exit_code == 0, result.output
    return fingerprint(published(work))


def test_kill_between_load_and_swap_leaves_the_published_file_unchanged(tmp_path, uninterrupted):
    first = roi(tmp_path, "build", "--config", str(FIXTURE))
    assert first.exit_code == 0, first.output
    before = file_digest(published(tmp_path))

    proc = killed_build(tmp_path)
    assert proc.returncode == -signal.SIGKILL, proc.stderr
    assert file_digest(published(tmp_path)) == before
    # the kill came after the load and before the models: the leftover holds the raw tables only
    assert "raw_ai_spend" in table_names(leftover(tmp_path))
    assert "fct_team_month" not in table_names(leftover(tmp_path))

    rerun = roi(tmp_path, "build", "--config", str(FIXTURE))
    assert rerun.exit_code == 0, rerun.output
    assert not leftover(tmp_path).exists()
    assert fingerprint(published(tmp_path)) == uninterrupted


def test_kill_before_anything_was_published_publishes_nothing(tmp_path, uninterrupted):
    proc = killed_build(tmp_path)
    assert proc.returncode == -signal.SIGKILL, proc.stderr
    assert not published(tmp_path).exists()
    assert leftover(tmp_path).exists()

    rerun = roi(tmp_path, "build", "--config", str(FIXTURE))
    assert rerun.exit_code == 0, rerun.output
    assert not leftover(tmp_path).exists()
    assert fingerprint(published(tmp_path)) == uninterrupted


def test_a_failed_dbt_test_leaves_the_published_file_unchanged(tmp_path):
    assert roi(tmp_path, "build", "--config", str(FIXTURE)).exit_code == 0
    before = file_digest(published(tmp_path))

    # one cent more in a ledger line of the books: the tie test of the models fails
    path = tmp_path / "data" / "books" / "raw_gl_journal.csv"
    with path.open(newline="") as f:
        rows = list(csv.reader(f))
    header = rows[0]
    target = next(r for r in rows[1:] if r[0] == "gl-support-2025-09-6100")
    target[header.index("debit_cents")] = str(int(target[header.index("debit_cents")]) + 1)
    with path.open("w", newline="") as f:
        csv.writer(f, lineterminator="\n").writerows(rows)

    result = roi(tmp_path, "build")  # no --config: the edited books are loaded as they are
    assert result.exit_code == 3, result.output
    assert file_digest(published(tmp_path)) == before
    assert not leftover(tmp_path).exists()
