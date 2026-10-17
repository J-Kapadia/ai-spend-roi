"""A roll-forward that fails its gate leaves the published warehouse untouched; the retry with the
corrected file succeeds and a repeat is a no-op."""

import csv
import hashlib
import shutil
from pathlib import Path

import pytest
from click.testing import CliRunner

from conftest import connect
from roi import rollforward
from roi.cli import main
from roi.gen import manifest

pytestmark = pytest.mark.slow

FIXTURE = Path(__file__).parent / "fixtures" / "gen_fixture.yaml"
ROI_FIXTURE = Path(__file__).parent / "fixtures" / "roi_fixture.yaml"
MONTH = "2026-04"
FILE = "raw_ai_spend"
KEY = manifest.incoming_path(MONTH, FILE)


def roi(work, *args):
    return CliRunner().invoke(main, ["--work-dir", str(work), *args])


def published(work) -> Path:
    return work / "warehouse" / "roi.duckdb"


def file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def output_months(work) -> set[str]:
    con = connect(str(published(work)), read_only=True)
    try:
        return {m for (m,) in con.execute("SELECT DISTINCT strftime(month, '%Y-%m') FROM raw_output").fetchall()}
    finally:
        con.close()


def break_by_a_cent(work) -> None:
    """One cent more on a row of the incoming spend file, and the copy of the manifest edited to match,
    so the ledger tie, not the manifest, is the gate that fails."""
    path = work / "data" / "incoming" / MONTH / f"{FILE}.csv"
    with path.open(newline="") as f:
        rows = list(csv.reader(f))
    col = rows[0].index("cost_cents")
    rows[1][col] = str(int(rows[1][col]) + 1)
    with path.open("w", newline="") as f:
        csv.writer(f, lineterminator="\n").writerows(rows)
    pinned = manifest.parse((work / "data" / manifest.INCOMING_NAME).read_text())
    pinned[KEY] = manifest.sha256_file(path)
    (work / "data" / manifest.INCOMING_NAME).write_text(manifest.render(pinned))


@pytest.fixture(scope="module")
def scenario(tmp_path_factory):
    work = tmp_path_factory.mktemp("scenario9")
    (work / "config").mkdir()
    shutil.copy(ROI_FIXTURE, work / "config" / "roi.yaml")
    assert roi(work, "build", "--config", str(FIXTURE)).exit_code == 0
    good_file = (work / "data" / "incoming" / MONTH / f"{FILE}.csv").read_bytes()
    good_manifest = (work / "data" / manifest.INCOMING_NAME).read_bytes()

    before = rollforward.content_hash(published(work))
    bytes_before = file_digest(published(work))
    marker_before = (work / "warehouse" / ".complete").read_text()

    break_by_a_cent(work)
    failed = roi(work, "rollforward", "--month", MONTH)
    return work, failed, before, bytes_before, marker_before, good_file, good_manifest


def test_the_failed_roll_forward_exits_3_and_leaves_the_published_file_alone(scenario):
    work, failed, before, bytes_before, marker_before, _, _ = scenario
    assert failed.exit_code == 3, failed.output
    assert file_digest(published(work)) == bytes_before
    assert rollforward.content_hash(published(work)) == before
    assert (work / "warehouse" / ".complete").read_text() == marker_before
    assert not (work / "warehouse" / "roi.staging.duckdb").exists()
    assert not (work / "warehouse" / "roi.prev.duckdb").exists()
    # no partial month is visible
    assert "2026-04" not in output_months(work)


def test_the_closed_month_still_verifies(scenario):
    work, _, before, *_ = scenario
    result = roi(work, "verify", "--through", "2026-03")
    assert result.exit_code == 0, result.output
    assert rollforward.content_hash(published(work)) == before


def test_the_retry_with_the_corrected_file_succeeds_and_the_repeat_is_a_no_op(scenario):
    work, _, before, _, _, good_file, good_manifest = scenario
    # the corrected file and its original manifest entry go back together
    (work / "data" / "incoming" / MONTH / f"{FILE}.csv").write_bytes(good_file)
    (work / "data" / manifest.INCOMING_NAME).write_bytes(good_manifest)
    retry = roi(work, "rollforward", "--month", MONTH)
    assert retry.exit_code == 0, retry.output
    assert "2026-04" in output_months(work)
    con = connect(str(published(work)), read_only=True)
    try:
        recorded = dict(con.execute("SELECT file, sha256 FROM rollforward_ingest").fetchall())
    finally:
        con.close()
    assert recorded[f"{FILE}.csv"] == manifest.parse(good_manifest.decode())[KEY]
    after = rollforward.content_hash(published(work))
    assert after != before

    repeat = roi(work, "rollforward", "--month", MONTH)
    assert repeat.exit_code == 0, repeat.output
    assert "already ingested, identical" in repeat.output
    assert rollforward.content_hash(published(work)) == after
    # a month once, not twice
    con = connect(str(published(work)), read_only=True)
    try:
        assert con.execute("SELECT count(*) FROM rollforward_ingest").fetchone()[0] == 4
        assert con.execute("SELECT count(*) FROM raw_output WHERE month = DATE '2026-04-01'").fetchone()[0] == 8
    finally:
        con.close()
    assert roi(work, "verify", "--through", MONTH).exit_code == 0
