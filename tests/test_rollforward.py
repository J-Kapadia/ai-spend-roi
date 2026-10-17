"""`roi rollforward` on the fixture company: Scenario 5, the incoming month the generator writes,
idempotency, out-of-order months, the manifest gate, the lock, the swap sequence and the truth boundary."""

import csv
import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from conftest import connect
from roi import rollforward
from roi.cli import main
from roi.gen import manifest

FIXTURE = Path(__file__).parent / "fixtures" / "gen_fixture.yaml"
ROI_FIXTURE = Path(__file__).parent / "fixtures" / "roi_fixture.yaml"
MONTH = "2026-04"
CLOSED = "2026-03"
FORBIDDEN = {"roi.truth", "roi.verify_truth", "roi.commands.verify"}

# Tables whose rows are keyed by the month they were computed through.
RESULT_TABLES = ("est_att_gt", "est_att_cov", "est_team_effect", "roi_team", "roi_sensitivity",
                 "fit_curve", "alloc_result", "agenda_item")

DUMP = """
import json, sys
from roi.cli import main
try:
    main(sys.argv[3:], standalone_mode=False)
except BaseException as e:
    print(f"{type(e).__name__}: {e}", file=sys.stderr)
finally:
    json.dump(sorted(m for m in sys.modules if m == "roi" or m.startswith("roi.")), open(sys.argv[2], "w"))
"""


def roi(work, *args):
    return CliRunner().invoke(main, ["--work-dir", str(work), *args])


def db(work) -> Path:
    return work / "warehouse" / "roi.duckdb"


def fetch(work, sql, *params, path=None):
    con = connect(str(path or db(work)), read_only=True)
    try:
        return con.execute(sql, list(params)).fetchall()
    finally:
        con.close()


def copy(src, dst) -> Path:
    shutil.copytree(src, dst, symlinks=True)
    return dst


def closed_rows(work, path=None) -> dict[str, list]:
    """Every row computed through a month up to the closed one, per table, in a fixed order."""
    out = {t: fetch(work, f"SELECT * FROM {t} WHERE through_month <= ? ORDER BY ALL", CLOSED, path=path)
           for t in RESULT_TABLES}
    out["prov_record"] = fetch(
        work, "SELECT * FROM prov_record WHERE through_month <= DATE '2026-03-01' ORDER BY ALL", path=path)
    return out


@pytest.fixture(scope="module")
def base(tmp_path_factory):
    """The fixture company built through 2026-03, with the roi config in the work dir."""
    work = tmp_path_factory.mktemp("base")
    (work / "config").mkdir()
    shutil.copy(ROI_FIXTURE, work / "config" / "roi.yaml")
    result = roi(work, "build", "--config", str(FIXTURE))
    assert result.exit_code == 0, result.output
    return work


@pytest.fixture(scope="module")
def rolled(base, tmp_path_factory):
    """Scenario 5: `base` rolled forward to 2026-04 in a process of its own, which records what it loaded."""
    work = copy(base, tmp_path_factory.mktemp("rolled") / "w")
    before = rollforward.content_hash(db(work))
    out = work / "modules.json"
    proc = subprocess.run(
        [sys.executable, "-c", DUMP, str(work), str(out), "--work-dir", str(work), "rollforward", "--month", MONTH],
        capture_output=True, text=True, check=False,
    )
    assert db(work).exists() and "rolled forward to" in proc.stdout, proc.stdout + proc.stderr
    return work, before, json.loads(out.read_text())


# ---- the incoming month the generator writes ---------------------------------------------------

def test_the_generator_writes_the_incoming_month_and_its_manifest(tmp_path):
    result = roi(tmp_path, "gen", "--config", str(FIXTURE))
    assert result.exit_code == 0, result.output
    month = tmp_path / "data" / "incoming" / MONTH
    assert sorted(p.name for p in month.iterdir()) == [
        "raw_ai_spend.csv", "raw_gl_journal.csv", "raw_hr_roster.csv", "raw_output.csv"]
    pinned = manifest.parse((tmp_path / "data" / manifest.INCOMING_NAME).read_text())
    assert pinned == {f"incoming/{MONTH}/{p.name}": hashlib.sha256(p.read_bytes()).hexdigest() for p in month.iterdir()}

    # the month's rows only, and the spend ties to the ledger line of each team to the cent
    with (month / "raw_ai_spend.csv").open(newline="") as f:
        spend = list(csv.DictReader(f))
    assert spend and {r["usage_date"][:7] for r in spend} == {MONTH}
    with (month / "raw_gl_journal.csv").open(newline="") as f:
        gl = [r for r in csv.DictReader(f) if r["account_code"] == "6100"]
    assert {r["period"] for r in gl} == {MONTH}
    by_team: dict[str, int] = {}
    for r in spend:
        by_team[r["team_id"]] = by_team.get(r["team_id"], 0) + int(r["cost_cents"])
    assert by_team == {r["cost_center"]: int(r["debit_cents"]) - int(r["credit_cents"]) for r in gl}
    with (month / "raw_output.csv").open(newline="") as f:
        assert {r["month"] for r in csv.DictReader(f)} == {f"{MONTH}-01"}


def test_the_incoming_month_is_deterministic_and_the_books_are_unchanged_by_it(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    assert roi(a, "gen", "--config", str(FIXTURE)).exit_code == 0
    assert roi(b, "gen", "--config", str(FIXTURE)).exit_code == 0
    for rel in (f"incoming/{MONTH}/raw_ai_spend.csv", "MANIFEST.incoming.sha256", "books/raw_output.csv"):
        assert (a / "data" / rel).read_bytes() == (b / "data" / rel).read_bytes()
    with (a / "data" / "books" / "raw_output.csv").open(newline="") as f:
        assert max(r["month"] for r in csv.DictReader(f)) == f"{CLOSED}-01"


# ---- Scenario 5 --------------------------------------------------------------------------------

def test_scenario_5_roll_forward_then_verify(rolled):
    work, _, _ = rolled
    result = roi(work, "verify", "--through", MONTH)
    assert result.exit_code == 0, result.output
    out = result.output
    assert "(a) GL tie: ok" in out and "(b) interval coverage: ok" in out and "(c) unmeasured discipline: ok" in out
    assert "measured (6): finance_operations, legal_operations, marketing, product_engineering," in out
    assert "unmeasured (1): platform_engineering (no_pre_period)" in out
    assert "control (1): people_operations" in out


def test_only_legal_operations_changes_status(base, rolled):
    work, _, _ = rolled
    statuses = {
        m: dict(fetch(work, "SELECT team_id, status FROM est_team_effect WHERE through_month = ?", m))
        for m in (CLOSED, MONTH)
    }
    flipped = {t for t in statuses[CLOSED] if statuses[CLOSED][t] != statuses[MONTH][t]}
    assert flipped == {"legal_operations"}
    assert statuses[CLOSED]["legal_operations"] == "unmeasured" and statuses[MONTH]["legal_operations"] == "measured"


def test_the_new_month_gains_rows_and_the_closed_months_are_unchanged(base, rolled):
    work, _, _ = rolled
    assert closed_rows(work) == closed_rows(base)
    for table in RESULT_TABLES:
        assert fetch(work, f"SELECT count(*) FROM {table} WHERE through_month = ?", MONTH)[0][0] > 0, table
    assert fetch(work, "SELECT count(*) FROM prov_record WHERE through_month = DATE '2026-04-01'")[0][0] > 0
    assert fetch(work, "SELECT max(strftime(month, '%Y-%m')) FROM raw_output")[0][0] == MONTH


def test_the_ingest_is_recorded_per_file_and_matches_the_manifest(rolled):
    work, _, _ = rolled
    recorded = {f: s for f, s in fetch(work, "SELECT file, sha256 FROM rollforward_ingest WHERE month = DATE '2026-04-01'")}
    pinned = manifest.parse((work / "data" / manifest.INCOMING_NAME).read_text())
    assert recorded == {k.rsplit("/", 1)[1]: v for k, v in pinned.items()}
    assert len(recorded) == 4


def test_the_swap_leaves_the_marker_the_previous_file_and_a_log(base, rolled):
    work, before, _ = rolled
    wh = work / "warehouse"
    assert (wh / ".complete").read_text().strip() == rollforward.content_hash(db(work))
    assert not (wh / ".complete.next").exists() and not (wh / "roi.staging.duckdb").exists()
    assert rollforward.content_hash(wh / "roi.prev.duckdb") == before
    assert before in (wh / "rollforward.log").read_text() and MONTH in (wh / "rollforward.log").read_text()
    # the new month is in the file, the previous file has none of it
    assert fetch(work, "SELECT count(*) FROM raw_output WHERE month = DATE '2026-04-01'", path=wh / "roi.prev.duckdb")[0][0] == 0


def test_a_repeat_is_a_no_op(rolled):
    work, _, _ = rolled
    before = rollforward.content_hash(db(work))
    result = roi(work, "rollforward", "--month", MONTH)
    assert result.exit_code == 0, result.output
    assert "already ingested, identical" in result.output
    assert rollforward.content_hash(db(work)) == before
    assert fetch(work, "SELECT count(*) FROM raw_output WHERE month = DATE '2026-04-01'")[0][0] == 8


def test_an_ingested_month_with_different_files_is_refused(rolled, tmp_path):
    work = copy(rolled[0], tmp_path / "w")
    path = work / "data" / "incoming" / MONTH / "raw_output.csv"
    path.write_text(path.read_text().replace("2026-04-01", "2026-04-01 ", 1))
    before = rollforward.content_hash(db(work))
    result = roi(work, "rollforward", "--month", MONTH)
    assert result.exit_code == 2
    assert "already ingested with different content; restore from seed to change history" in result.output
    assert rollforward.content_hash(db(work)) == before


def test_a_roll_forward_leaves_the_truth_modules_unloaded(rolled):
    _, _, loaded = rolled
    assert {"roi.commands.rollforward", "roi.rollforward", "roi.verify_books", "roi.estimator"} <= set(loaded)
    assert not set(loaded) & FORBIDDEN, sorted(set(loaded) & FORBIDDEN)


# ---- out of order, missing, tampered -----------------------------------------------------------

@pytest.mark.parametrize("month, message", [
    ("2026-05", "expected 2026-04 after 2026-03"),
    ("2026-03", "expected 2026-04 after 2026-03"),
    ("2025-01", "expected 2026-04 after 2026-03"),
])
def test_a_month_out_of_order_is_a_usage_error(base, tmp_path, month, message):
    work = copy(base, tmp_path / "w")
    before = rollforward.content_hash(db(work))
    result = roi(work, "rollforward", "--month", month)
    assert result.exit_code == 2, result.output
    assert message in result.output
    assert rollforward.content_hash(db(work)) == before
    assert not (work / "warehouse" / "roi.staging.duckdb").exists()


@pytest.mark.parametrize("month", ["April", "2026-13", "2026-4"])
def test_a_bad_month_is_a_usage_error(base, tmp_path, month):
    assert roi(copy(base, tmp_path / "w"), "rollforward", "--month", month).exit_code == 2


def test_missing_incoming_files_are_a_usage_error(base, tmp_path):
    work = copy(base, tmp_path / "w")
    (work / "data" / "incoming" / MONTH / "raw_output.csv").unlink()
    result = roi(work, "rollforward", "--month", MONTH)
    assert result.exit_code == 2 and "raw_output.csv" in result.output


def test_no_warehouse_is_a_usage_error(tmp_path):
    assert roi(tmp_path, "rollforward", "--month", MONTH).exit_code == 2


def test_a_changed_file_fails_the_manifest_before_anything_is_copied(base, tmp_path):
    work = copy(base, tmp_path / "w")
    path = work / "data" / "incoming" / MONTH / "raw_ai_spend.csv"
    path.write_text(path.read_text() + "\n")
    before = rollforward.content_hash(db(work))
    result = roi(work, "rollforward", "--month", MONTH)
    assert result.exit_code == 3, result.output
    assert f"incoming/{MONTH}/raw_ai_spend.csv does not match MANIFEST.incoming.sha256" in result.output
    assert rollforward.content_hash(db(work)) == before
    assert not (work / "warehouse" / "roi.staging.duckdb").exists()


def test_a_month_missing_from_the_manifest_fails_it(base, tmp_path):
    work = copy(base, tmp_path / "w")
    (work / "data" / manifest.INCOMING_NAME).write_text("")
    result = roi(work, "rollforward", "--month", MONTH)
    assert result.exit_code == 3 and "is not in MANIFEST.incoming.sha256" in result.output
    (work / "data" / manifest.INCOMING_NAME).unlink()
    assert roi(work, "rollforward", "--month", MONTH).exit_code == 3


def test_a_published_file_that_does_not_match_its_marker_is_refused(base, tmp_path):
    work = copy(base, tmp_path / "w")
    con = connect(str(db(work)))
    con.execute("UPDATE raw_gl_journal SET debit_cents = debit_cents + 1 WHERE account_code <> '6100'")
    con.close()
    result = roi(work, "rollforward", "--month", MONTH)
    assert result.exit_code == 3 and "does not match" in result.output


def test_a_second_process_on_the_work_dir_finds_the_warehouse_busy(base, tmp_path):
    work = copy(base, tmp_path / "w")
    with (work / "warehouse" / ".lock").open("a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        result = roi(work, "rollforward", "--month", MONTH)
    assert result.exit_code == 2 and "warehouse busy" in result.output


# ---- the swap sequence -------------------------------------------------------------------------

def tiny(path: Path, rows: list[tuple], build_facts: str = "x") -> None:
    con = connect(str(path))
    con.execute("CREATE TABLE t(a INTEGER, b DOUBLE, d DATE)")
    con.executemany("INSERT INTO t VALUES (?, ?, ?)", rows)
    con.execute("CREATE TABLE prov_build(note VARCHAR)")
    con.execute("INSERT INTO prov_build VALUES (?)", [build_facts])
    con.close()


def test_the_content_hash_ignores_row_order_build_facts_and_bytes(tmp_path):
    rows = [(1, 0.5, "2026-01-01"), (2, 1.25, "2026-02-01")]
    tiny(tmp_path / "a.duckdb", rows, "one")
    tiny(tmp_path / "b.duckdb", rows[::-1], "two")
    tiny(tmp_path / "c.duckdb", [(1, 0.5, "2026-01-01"), (2, 1.2500001, "2026-02-01")])
    assert rollforward.content_hash(tmp_path / "a.duckdb") == rollforward.content_hash(tmp_path / "b.duckdb")
    assert rollforward.content_hash(tmp_path / "a.duckdb") != rollforward.content_hash(tmp_path / "c.duckdb")


def stage(tmp_path, rows=((1, 0.5, "2026-01-01"),)):
    work = rollforward.WorkDir(tmp_path)
    work.warehouse.mkdir()
    tiny(work.warehouse_db, list(rows))
    return work


def test_publish_runs_the_four_steps_and_keeps_the_previous_file(tmp_path):
    work = stage(tmp_path)
    old = rollforward.content_hash(work.warehouse_db)
    tiny(work.warehouse_next_db, [(2, 1.5, "2026-02-01")])
    new = rollforward.publish(work, work.warehouse_next_db, keep_previous=True)
    assert new == rollforward.content_hash(work.warehouse_db) == rollforward.read_marker(work)
    assert rollforward.content_hash(work.warehouse / "roi.prev.duckdb") == old
    assert not work.warehouse_next_db.exists() and not rollforward.marker_next_path(work).exists()


def test_a_marker_behind_the_file_is_promoted_and_a_stale_pending_marker_dropped(tmp_path, monkeypatch):
    work = stage(tmp_path)
    (work.warehouse / ".complete").write_text(rollforward.content_hash(work.warehouse_db) + "\n")
    tiny(work.warehouse_next_db, [(2, 1.5, "2026-02-01")])

    def killed(_work):
        raise KeyboardInterrupt  # stands for the process dying between steps 3 and 4

    monkeypatch.setattr(rollforward, "swap_marker", killed)
    with pytest.raises(KeyboardInterrupt):
        rollforward.publish(work, work.warehouse_next_db, keep_previous=False)
    published = rollforward.content_hash(work.warehouse_db)
    assert rollforward.read_marker(work) != published
    assert rollforward.marker_next_path(work).read_text().strip() == published
    with pytest.raises(rollforward.RoiError):
        rollforward.check_marker(work)

    assert rollforward.promote_pending(work) is True
    assert rollforward.read_marker(work) == published and not rollforward.marker_next_path(work).exists()
    assert rollforward.check_marker(work) == published

    # killed between steps 2 and 3: the pending marker names a file that was never swapped in
    rollforward.marker_next_path(work).write_text("0" * 64 + "\n")
    assert rollforward.promote_pending(work) is False
    assert not rollforward.marker_next_path(work).exists() and rollforward.read_marker(work) == published


# ---- a rebuild recreates a rolled-forward warehouse --------------------------------------------

@pytest.mark.slow
def test_build_through_replays_the_incoming_months_to_the_same_warehouse(rolled, tmp_path):
    work, _, _ = rolled
    other = tmp_path / "w"
    (other / "config").mkdir(parents=True)
    shutil.copy(ROI_FIXTURE, other / "config" / "roi.yaml")
    result = roi(other, "build", "--config", str(FIXTURE), "--through", MONTH)
    assert result.exit_code == 0, result.output
    assert rollforward.content_hash(db(other)) == rollforward.content_hash(db(work))
    assert (other / "warehouse" / ".complete").read_text() == (work / "warehouse" / ".complete").read_text()
    assert roi(other, "verify", "--through", MONTH).exit_code == 0


def test_build_through_needs_incoming_data_for_the_month(base, tmp_path):
    work = copy(base, tmp_path / "w")
    before = rollforward.content_hash(db(work))
    assert roi(work, "build", "--through", "2026-05").exit_code == 2
    assert roi(work, "build", "--through", "2026-02").exit_code == 2
    assert roi(work, "build", "--through", "April").exit_code == 2
    assert rollforward.content_hash(db(work)) == before
