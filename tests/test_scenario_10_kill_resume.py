"""A build killed between the load and the file swap leaves the published file alone,
and the next build resumes to the same warehouse as an uninterrupted one. A roll-forward killed
before the swap leaves the published file alone; killed between the file swap and the marker swap
it leaves the new month in place, and the next command promotes the marker."""

import csv
import hashlib
import shutil
import signal
import subprocess
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from conftest import connect
from roi import rollforward
from roi.cli import main

pytestmark = pytest.mark.slow

FIXTURE = Path(__file__).parent / "fixtures" / "gen_fixture.yaml"
ROI_FIXTURE = Path(__file__).parent / "fixtures" / "roi_fixture.yaml"

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
    con = connect(str(path), read_only=True)
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
    con = connect(str(path), read_only=True)
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


# ---- the roll-forward --------------------------------------------------------------------------

# Run a command with one function of the roll-forward replaced by a SIGKILL of the process itself.
KILL_AT = """
import os, signal, sys
import roi.rollforward as rf
setattr(rf, sys.argv[1], lambda *a, **k: os.kill(os.getpid(), signal.SIGKILL))
from roi.cli import main
main(sys.argv[2:])
"""


def killed(work, function, *args) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", KILL_AT, function, "--work-dir", str(work), *args],
        capture_output=True, text=True, check=False,
    )


def state(work) -> tuple[str, str | None]:
    """The content hash of the published file and the marker beside it."""
    marker = work / "warehouse" / ".complete"
    return rollforward.content_hash(published(work)), marker.read_text().strip() if marker.exists() else None


@pytest.fixture(scope="module")
def base(tmp_path_factory):
    work = tmp_path_factory.mktemp("rf_base")
    (work / "config").mkdir()
    shutil.copy(ROI_FIXTURE, work / "config" / "roi.yaml")
    assert roi(work, "build", "--config", str(FIXTURE)).exit_code == 0
    return work


@pytest.fixture(scope="module")
def rolled(base, tmp_path_factory):
    """The content hash of the base warehouse rolled forward without interruption."""
    work = tmp_path_factory.mktemp("rf_rolled") / "w"
    shutil.copytree(base, work, symlinks=True)
    result = roi(work, "rollforward", "--month", "2026-04")
    assert result.exit_code == 0, result.output
    return state(work)[0]


@pytest.fixture
def work(base, tmp_path):
    shutil.copytree(base, tmp_path / "w", symlinks=True)
    return tmp_path / "w"


def test_a_roll_forward_killed_before_the_swap_leaves_the_published_file_unchanged(work, rolled):
    before, marker = state(work)
    file_before = file_digest(published(work))
    proc = killed(work, "publish", "rollforward", "--month", "2026-04")
    assert proc.returncode == -signal.SIGKILL, proc.stderr
    assert file_digest(published(work)) == file_before
    assert state(work) == (before, marker) and marker == before
    staging = work / "warehouse" / "roi.staging.duckdb"
    assert staging.exists()  # the finished month, never published

    rerun = roi(work, "rollforward", "--month", "2026-04")
    assert rerun.exit_code == 0, rerun.output
    assert not staging.exists()
    assert state(work) == (rolled, rolled)


def test_a_roll_forward_killed_between_the_file_swap_and_the_marker_swap_is_promoted_by_the_next_command(work, rolled):
    old, _ = state(work)
    proc = killed(work, "swap_marker", "rollforward", "--month", "2026-04")
    assert proc.returncode == -signal.SIGKILL, proc.stderr
    wh = work / "warehouse"
    # the file swap happened: the new month is in the published file, the marker is behind it
    new, marker = state(work)
    assert new == rolled and marker == old
    assert (wh / ".complete.next").read_text().strip() == rolled
    assert not (wh / "roi.staging.duckdb").exists()
    assert rollforward.content_hash(wh / "roi.prev.duckdb") == old

    result = roi(work, "verify", "--through", "2026-04")  # the next command
    assert result.exit_code == 0, result.output
    assert state(work) == (rolled, rolled)
    assert not (wh / ".complete.next").exists()

    again = roi(work, "rollforward", "--month", "2026-04")
    assert again.exit_code == 0 and "already ingested, identical" in again.output
    assert state(work) == (rolled, rolled)


def test_a_roll_forward_retried_after_a_kill_between_the_swaps_is_a_no_op(work, rolled):
    killed(work, "swap_marker", "rollforward", "--month", "2026-04")
    again = roi(work, "rollforward", "--month", "2026-04")
    assert again.exit_code == 0, again.output
    assert "already ingested, identical" in again.output
    assert state(work) == (rolled, rolled)


def test_a_build_killed_between_the_file_swap_and_the_marker_swap_is_promoted_too(tmp_path, uninterrupted):
    proc = killed(tmp_path, "swap_marker", "build", "--config", str(FIXTURE))
    assert proc.returncode == -signal.SIGKILL, proc.stderr
    assert published(tmp_path).exists() and not leftover(tmp_path).exists()
    assert (tmp_path / "warehouse" / ".complete.next").exists() and not (tmp_path / "warehouse" / ".complete").exists()

    # a build promotes the leftover marker, then rebuilds: the same warehouse as an uninterrupted one
    rerun = roi(tmp_path, "build", "--config", str(FIXTURE))
    assert rerun.exit_code == 0, rerun.output
    assert fingerprint(published(tmp_path)) == uninterrupted
    assert not (tmp_path / "warehouse" / ".complete.next").exists()
    assert (tmp_path / "warehouse" / ".complete").read_text().strip() == rollforward.content_hash(published(tmp_path))
