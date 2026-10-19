"""`--json` on build, gen, verify, memo, rollforward and signoff: one JSON object on stdout, the exit code of
the error contract unchanged, and the text output of a run without `--json` as it was."""

import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from conftest import connect
from roi.cli import main

FIXTURES = Path(__file__).parent / "fixtures"
GEN_FIXTURE = FIXTURES / "gen_fixture.yaml"
MONTH = "2026-03"
NEXT = "2026-04"
KEYS = {"command", "status", "checks", "artifacts"}


def roi(work, *args, **kwargs):
    return CliRunner().invoke(main, ["--work-dir", str(work), *args], **kwargs)


def report(result, command, status="pass", exit_code=0):
    """The one JSON object on stdout, after checking its shape, the status and the exit code."""
    assert result.exit_code == exit_code, result.output
    doc = json.loads(result.stdout)  # json.loads accepts one document only
    assert set(doc) == KEYS
    assert doc["command"] == command and doc["status"] == status
    for c in doc["checks"]:
        assert set(c) == {"id", "pass", "detail"} and isinstance(c["pass"], bool)
    assert all(isinstance(a, str) for a in doc["artifacts"])
    return doc


def failed(doc):
    return [c["id"] for c in doc["checks"] if not c["pass"]]


def ids(doc):
    return [c["id"] for c in doc["checks"]]


def edit(work, *statements):
    con = connect(str(work / "warehouse" / "roi.duckdb"))
    try:
        for sql in statements:
            con.execute(sql)
    finally:
        con.close()


@pytest.fixture(scope="module")
def company(tmp_path_factory):
    """The fixture company built through 2026-03 and its memo, each run with --json."""
    work = tmp_path_factory.mktemp("company")
    (work / "config").mkdir()
    shutil.copy(FIXTURES / "roi_fixture.yaml", work / "config" / "roi.yaml")
    build = roi(work, "build", "--config", str(GEN_FIXTURE), "--json")
    assert build.exit_code == 0, build.output
    memo = roi(work, "memo", "--month", MONTH, "--check", "--json")
    assert memo.exit_code == 0, memo.output
    return SimpleNamespace(work=work, build=build, memo=memo)


@pytest.fixture
def copy(company, tmp_path):
    shutil.copytree(company.work, tmp_path / "w", symlinks=True)
    return tmp_path / "w"


# ---- build -------------------------------------------------------------------------------------

def test_build_reports_a_check_per_step_and_the_warehouse(company):
    doc = report(company.build, "build")
    assert {"manifest", "step:load", "step:dbt"} <= set(ids(doc))
    assert doc["artifacts"] == [str(company.work / "warehouse" / "roi.duckdb")]


def test_build_with_a_bad_month_is_a_failed_check_with_exit_2(tmp_path):
    result = roi(tmp_path, "build", "--toy", "--through", "2026-13", "--json")
    doc = report(result, "build", "error", 2)
    assert failed(doc) == ["error"] and "2026-13" in doc["checks"][-1]["detail"]
    assert "error[2]" in result.stderr


# ---- gen ---------------------------------------------------------------------------------------

def test_gen_reports_the_books_the_manifests_and_the_truth(tmp_path):
    doc = report(roi(tmp_path, "gen", "--config", str(GEN_FIXTURE), "--json"), "gen")
    names = {Path(a).name for a in doc["artifacts"]}
    assert {"raw_ai_spend.csv", "raw_output.csv", "MANIFEST.sha256", "MANIFEST.incoming.sha256", "truth.json"} <= names
    assert all(Path(a).exists() for a in doc["artifacts"])


def test_gen_without_its_config_is_a_failed_check_with_exit_2(tmp_path):
    doc = report(roi(tmp_path, "gen", "--config", str(tmp_path / "missing.yaml"), "--json"), "gen", "error", 2)
    assert failed(doc) == ["error"] and doc["artifacts"] == []


# ---- verify ------------------------------------------------------------------------------------

def test_verify_reports_the_tie_each_team_and_each_registered_check(company):
    doc = report(roi(company.work, "verify", "--through", MONTH, "--json"), "verify")
    assert "gl_tie" in ids(doc) and "allocation_degenerate" in ids(doc)
    assert any(i.startswith("coverage:") for i in ids(doc))
    assert len(ids(doc)) > 4  # beyond the tie, the teams and the degenerate-allocation check: the registry's
    assert failed(doc) == []


def test_verify_text_output_is_the_same_with_and_without_json(company):
    text = roi(company.work, "verify", "--through", MONTH)
    as_json = roi(company.work, "verify", "--through", MONTH, "--json")
    assert text.exit_code == 0 and as_json.exit_code == 0
    assert text.stdout.startswith("verify through ")
    assert as_json.stderr == text.stdout  # the text a run prints goes to stderr under --json
    assert text.stdout not in as_json.stdout


def test_verify_without_a_warehouse_is_a_failed_check_with_exit_2(tmp_path):
    doc = report(roi(tmp_path, "verify", "--through", MONTH, "--json"), "verify", "error", 2)
    assert failed(doc) == ["error"]


def test_verify_after_a_one_cent_ledger_edit_exits_3(copy):
    edit(copy, "UPDATE raw_gl_journal SET debit_cents = debit_cents + 1 WHERE entry_id = 'gl-support-2025-09-6100'")
    doc = report(roi(copy, "verify", "--through", MONTH, "--json"), "verify", "error", 3)
    assert failed(doc) == ["error"] and "GL tie failed" in doc["checks"][-1]["detail"]


def test_verify_with_a_planted_effect_outside_its_interval_is_a_failed_check_with_exit_1(copy):
    path = copy / "truth" / "truth.json"
    truth = json.loads(path.read_text())
    effects = truth["teams"]["marketing"]["planted_effect_by_month"]
    truth["teams"]["marketing"]["planted_effect_by_month"] = {m: v + 1.0 for m, v in effects.items()}
    path.write_text(json.dumps(truth))
    doc = report(roi(copy, "verify", "--through", MONTH, "--json"), "verify", "fail", 1)
    assert failed(doc) == ["coverage:marketing"]


# ---- memo --------------------------------------------------------------------------------------

def test_memo_reports_the_tie_the_resolved_sections_and_every_page(company):
    doc = report(company.memo, "memo")
    assert "tie" in ids(doc) and "signoff" in ids(doc)
    assert any(i.startswith("resolved:") for i in ids(doc))
    pages = {Path(a).relative_to(company.work / "site").as_posix() for a in doc["artifacts"]}
    assert "index.html" in pages and any(p.startswith("prov/") for p in pages)
    assert all(Path(a).exists() for a in doc["artifacts"])


def test_memo_with_a_bad_month_exits_2(company):
    report(roi(company.work, "memo", "--month", "2026-13", "--json"), "memo", "error", 2)


def test_memo_after_a_one_cent_ledger_edit_exits_3(copy):
    edit(copy, "UPDATE raw_gl_journal SET debit_cents = debit_cents + 1 WHERE entry_id = (SELECT min(entry_id) FROM"
               " raw_gl_journal WHERE account_code = (SELECT value FROM roi_settings WHERE key = 'gl_account')"
               " AND debit_cents > 0)")
    doc = report(roi(copy, "memo", "--month", MONTH, "--json"), "memo", "error", 3)
    assert failed(doc) == ["error"]


def test_memo_requiring_a_signoff_that_is_pending_is_a_failed_check_with_exit_1(copy):
    doc = report(roi(copy, "memo", "--month", MONTH, "--require-signoff", "--json"), "memo", "fail", 1)
    assert failed(doc) == ["signoff"]


# ---- rollforward -------------------------------------------------------------------------------

def test_rollforward_reports_its_four_checks_and_the_warehouse(company, tmp_path):
    shutil.copytree(company.work, tmp_path / "w", symlinks=True)
    doc = report(roi(tmp_path / "w", "rollforward", "--month", NEXT, "--json"), "rollforward")
    assert ids(doc) == ["manifest", "dbt", "gl_tie", "swap"]
    assert doc["artifacts"] == [str(tmp_path / "w" / "warehouse" / "roi.duckdb")]


def test_rollforward_out_of_order_is_a_failed_check_with_exit_2(copy):
    doc = report(roi(copy, "rollforward", "--month", "2026-06", "--json"), "rollforward", "error", 2)
    assert failed(doc) == ["error"] and "expected 2026-04" in doc["checks"][-1]["detail"]


# ---- signoff -----------------------------------------------------------------------------------

def test_signoff_by_a_person_at_a_terminal_reports_the_file(copy, monkeypatch):
    monkeypatch.setattr("roi.commands.signoff._interactive", lambda: True)
    result = roi(copy, "signoff", "--month", MONTH, "--json", input="A. Person\ny\n")
    doc = report(result, "signoff")
    assert ids(doc) == ["memo", "signoff"]
    assert doc["artifacts"] == [str(copy / "signoffs" / f"{MONTH}.yaml")]
    assert Path(doc["artifacts"][0]).exists()


def test_signoff_refuses_a_non_interactive_run_as_a_failed_check_with_exit_2(copy):
    doc = report(roi(copy, "signoff", "--month", MONTH, "--json", input="A. Person\ny\n"), "signoff", "error", 2)
    assert failed(doc) == ["error"] and "needs a person at a terminal" in doc["checks"][-1]["detail"]
    assert not (copy / "signoffs").exists()
