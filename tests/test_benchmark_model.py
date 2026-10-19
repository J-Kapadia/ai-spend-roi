"""The reference table of the Ramp AI Index and the mart that sets it beside the company, on the hand-worked
books of test_dbt_models: months 2025-01..03, team `a` first spends in 2025-02, team `b` never."""

from datetime import date
from decimal import Decimal

import pytest
from click.testing import CliRunner

from roi.cli import main
from roi.steps import load
from roi.workdir import RoiError, WorkDir
from test_dbt_models import build_models, rows, write_books
from test_readonly_commands import content_hash

HEADER = "metric,period,value,unit,source_url,retrieved_on,notes"
INDEX = [
    HEADER,
    "paid_adoption_share,2024-02,10.0,percent,https://ramp.com/fixture,2026-10-18,",
    "paid_adoption_share,2025-01,40.0,percent,https://ramp.com/fixture,2026-10-18,",
    "paid_adoption_share,2025-02,41.5,percent,https://ramp.com/fixture,2026-10-18,",
]
COLUMNS = ["metric", "period", "value", "unit", "source_url", "retrieved_on", "notes"]


def write_reference(work, lines=INDEX):
    directory = work / "data" / "reference"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "ramp_ai_index.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")


def d(y, m):
    return date(y, m, 1)


def mart(db):
    return rows(db, "SELECT month, metric, index_period, index_value, index_unit, prior_period, prior_value,"
                    " source_url, retrieved_on, teams, teams_with_ai, company_share"
                    " FROM fct_benchmark_ramp ORDER BY month, metric")


def test_each_month_gets_the_latest_index_period_beside_the_company_share(tmp_path):
    write_books(tmp_path)
    write_reference(tmp_path)
    got = mart(build_models(tmp_path))
    url, got_on = "https://ramp.com/fixture", date(2026, 10, 18)
    assert got == [
        (d(2025, 1), "paid_adoption_share", "2025-01", Decimal("40.0"), "percent", None, None, url, got_on,
         2, 0, Decimal("0")),
        (d(2025, 2), "paid_adoption_share", "2025-02", Decimal("41.5"), "percent", "2024-02", Decimal("10.0"),
         url, got_on, 2, 1, Decimal("0.5")),
        (d(2025, 3), "paid_adoption_share", "2025-02", Decimal("41.5"), "percent", "2024-02", Decimal("10.0"),
         url, got_on, 2, 1, Decimal("0.5")),
    ]


def test_staging_keys_each_metric_and_period_and_parses_the_month(tmp_path):
    write_books(tmp_path)
    write_reference(tmp_path)
    got = rows(build_models(tmp_path), "SELECT key, month FROM stg_ramp_ai_index ORDER BY key")
    assert got == [("paid_adoption_share/2024-02", d(2024, 2)), ("paid_adoption_share/2025-01", d(2025, 1)),
                   ("paid_adoption_share/2025-02", d(2025, 2))]


def test_without_a_file_the_table_is_empty_and_the_models_still_build(tmp_path, monkeypatch):
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setattr(load, "REPO_REFERENCE_DIR", empty)
    work = tmp_path / "work"
    write_books(work)
    db = build_models(work)
    assert [c for (c, *_rest) in rows(db, "DESCRIBE ref_ramp_ai_index")] == COLUMNS
    assert rows(db, "SELECT count(*) FROM ref_ramp_ai_index") == [(0,)]
    assert rows(db, "SELECT count(*) FROM fct_benchmark_ramp") == [(0,)]


def test_the_work_dir_file_wins_over_the_repository_file(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "ramp_ai_index.csv").write_text(HEADER + "\n", encoding="utf-8")
    monkeypatch.setattr(load, "REPO_REFERENCE_DIR", repo)
    work = WorkDir(tmp_path / "work")
    assert load.reference_file(work, "ramp_ai_index.csv") == repo / "ramp_ai_index.csv"
    write_reference(tmp_path / "work")
    assert load.reference_file(work, "ramp_ai_index.csv") == work.data / "reference" / "ramp_ai_index.csv"
    assert load.reference_file(work, "absent.csv") is None


def test_a_file_with_the_wrong_header_is_refused_naming_the_file(tmp_path):
    write_books(tmp_path)
    write_reference(tmp_path, ["metric,period,value", "paid_adoption_share,2025-01,40.0"])
    with pytest.raises(RoiError) as err:
        build_models(tmp_path)
    assert err.value.exit_code == 3
    assert "ramp_ai_index.csv: expected columns" in err.value.message


def test_the_toy_build_and_the_memo_check_still_pass(tmp_path):
    runner = CliRunner()
    built = runner.invoke(main, ["--work-dir", str(tmp_path), "build", "--toy"])
    assert built.exit_code == 0, built.output
    checked = runner.invoke(main, ["--work-dir", str(tmp_path), "memo", "--month", "2025-06", "--check"])
    assert checked.exit_code == 0, checked.output


def test_two_builds_of_the_same_books_and_file_have_the_same_content_hash(tmp_path):
    hashes = []
    for name in ("one", "two"):
        work = tmp_path / name
        write_books(work)
        write_reference(work)
        hashes.append(content_hash(build_models(work)))
    assert hashes[0] == hashes[1]
