"""`roi explain`: the provenance record behind one number, re-checked against the warehouse."""

import json
import shutil

import pytest
import yaml
from click.testing import CliRunner

from conftest import connect
from roi import memo
from roi.cli import main
from roi.workdir import WorkDir

MONTH = "2025-06"
ROI_YAML = """
memo_budget_usd: 1000
value_per_unit:
  alpha: {low: 5.0, base: 10.0, high: 15.0}
  beta: {low: 5.0, base: 10.0, high: 15.0}
  gamma: {low: 5.0, base: 10.0, high: 15.0}
"""


def roi(work, *args):
    return CliRunner().invoke(main, ["--work-dir", str(work), *args])


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    work = tmp_path_factory.mktemp("explain")
    (work / "config").mkdir()
    (work / "config" / "roi.yaml").write_text(ROI_YAML)
    assert roi(work, "build", "--toy").exit_code == 0
    assert roi(work, "memo", "--month", MONTH).exit_code == 0
    return work


@pytest.fixture
def copy(built, tmp_path):
    shutil.copytree(built, tmp_path / "w", symlinks=True)
    return tmp_path / "w"


def ids(work) -> dict[str, str]:
    """display text -> id for the tagged numbers of the memo, by record name."""
    con = memo.open_warehouse(WorkDir(work))
    try:
        _, c = memo.collect(WorkDir(work), MONTH, con)
    finally:
        con.close()
    return {r.name: r.prov_id for r in c.records.values()}


def test_a_stored_record_prints_its_fields_and_its_build_facts(built):
    pid = ids(built)["company_ai_spend_usd"]
    result = roi(built, "explain", pid)
    assert result.exit_code == 0, result.output
    out = result.output
    assert f"({pid})" in out and "company_ai_spend_usd" in out
    assert "source: fct_team_month where month = DATE '2025-06-01'" in out
    assert "step: provenance" in out
    assert "script roi build" in out and "checks dbt build" in out
    assert "re-check: ok" in out and "sign-off: none" in out


def test_a_derived_record_says_it_has_no_stored_build(built):
    pid = ids(built)["effect:beta"]
    out = roi(built, "explain", pid).output
    assert "step: memo" in out and "source: est_team_effect where" in out
    assert "none stored" in out


def test_a_config_assumption_is_explained_from_the_config_file(built):
    pid = ids(built)["value_base:beta"]
    out = roi(built, "explain", pid).output
    assert "source: config where roi.yaml: value_per_unit.beta.base" in out and "rows: 1" in out
    assert "re-check: ok" in out


def test_a_unique_prefix_works_and_a_page_name_is_accepted(built):
    pid = ids(built)["effect:beta"]
    assert roi(built, "explain", pid[:8]).exit_code == 0
    assert roi(built, "explain", f"prov/{pid}.html").exit_code == 0


def test_json_output_carries_the_record_and_the_check(built):
    pid = ids(built)["company_ai_spend_usd"]
    data = json.loads(roi(built, "explain", pid, "--json").output)
    assert data["command"] == "explain" and data["status"] == "pass"
    assert data["checks"][0]["id"] == "recheck" and data["checks"][0]["pass"] is True
    assert data["record"]["prov_id"] == pid and data["record"]["stored"] is True
    assert data["record"]["builds"][0]["git_sha"]


def test_an_unknown_or_too_short_id_is_a_usage_error(built):
    unknown = roi(built, "explain", "feedfacefeedface")
    assert unknown.exit_code == 2 and "no provenance record starts with feedfacefeedface" in unknown.output
    short = roi(built, "explain", "ab")
    assert short.exit_code == 2 and "too short" in short.output


def test_a_changed_source_row_fails_the_recheck_with_exit_1(copy):
    pid = ids(copy)["company_ai_spend_usd"]
    con = connect(str(copy / "warehouse" / "roi.duckdb"))
    con.execute("UPDATE fct_team_month SET spend_cents = spend_cents + 1 WHERE team_id = 'alpha' AND month = DATE '2025-06-01'")
    con.close()
    result = roi(copy, "explain", pid)
    assert result.exit_code == 1
    assert "re-check: FAILED" in result.output and "checksum mismatch" in result.output


def test_a_signoff_that_cites_the_number_is_shown(copy):
    pid = ids(copy)["company_ai_spend_usd"]
    (copy / "signoffs").mkdir()
    (copy / "signoffs" / f"{MONTH}.yaml").write_text(yaml.safe_dump(
        {"month": MONTH, "name": "A. Person", "date": "2026-10-13", "memo_sha256": "x", "prov_ids": [pid]}))
    out = roi(copy, "explain", pid).output
    assert f"sign-off: signoffs/{MONTH}.yaml, A. Person on 2026-10-13" in out
    other = ids(copy)["effect:beta"]
    assert "sign-off: none" in roi(copy, "explain", other).output


def test_explain_without_a_warehouse_is_a_usage_error(tmp_path):
    result = roi(tmp_path, "explain", "abcdef0123456789")
    assert result.exit_code == 2 and result.output.startswith("error[2]:")


def test_explain_leaves_the_warehouse_file_unchanged(built):
    import hashlib

    db = built / "warehouse" / "roi.duckdb"
    before = hashlib.sha256(db.read_bytes()).hexdigest()
    assert roi(built, "explain", ids(built)["effect:beta"]).exit_code == 0
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before
