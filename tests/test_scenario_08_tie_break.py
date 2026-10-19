"""Spend and the ledger disagree by one cent: `roi verify` refuses with exit 3 and names the team-month."""

import hashlib
import shutil
from pathlib import Path

import pytest
from click.testing import CliRunner

from conftest import connect
from roi.cli import main

FIXTURES = Path(__file__).parent / "fixtures"
FIXTURE = FIXTURES / "gen_fixture.yaml"
THROUGH = "2026-03"
# One AI-usage ledger line of the fixture books, and the export lines it ties to.
GL_ENTRY = "gl-support-2025-09-6100"
GL_KEY = ("support", "2025-09")
UNASSIGNED_ENTRY = "gl-unassigned-2025-09-6100"


def roi(work, *args):
    return CliRunner().invoke(main, ["--work-dir", str(work), *args])


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    work = tmp_path_factory.mktemp("built")
    # the memo needs the fixture company's value ranges beside the books
    (work / "config").mkdir()
    shutil.copy(FIXTURES / "roi_fixture.yaml", work / "config" / "roi.yaml")
    result = roi(work, "build", "--config", str(FIXTURE))
    assert result.exit_code == 0, result.output
    return work


@pytest.fixture
def copy(built, tmp_path):
    shutil.copytree(built, tmp_path / "w", symlinks=True)
    return tmp_path / "w"


def edit(work, sql, *params):
    con = connect(str(work / "warehouse" / "roi.duckdb"))
    try:
        con.execute(sql, list(params))
    finally:
        con.close()


def fetch(work, sql, *params):
    con = connect(str(work / "warehouse" / "roi.duckdb"), read_only=True)
    try:
        return con.execute(sql, list(params)).fetchall()
    finally:
        con.close()


def month_spend(work, team, month):
    return fetch(
        work,
        "SELECT sum(cost_cents) FROM raw_ai_spend WHERE team_id = ? AND strftime(usage_date, '%Y-%m') = ?",
        team, month,
    )[0][0]


def test_untouched_books_verify(built):
    result = roi(built, "verify", "--through", THROUGH)
    assert result.exit_code == 0, result.output
    assert "(a) GL tie: ok" in result.output


def test_one_cent_in_the_ledger_is_refused_naming_the_team_month(copy):
    team, month = GL_KEY
    spend = month_spend(copy, team, month)
    edit(copy, "UPDATE raw_gl_journal SET debit_cents = debit_cents + 1 WHERE entry_id = ?", GL_ENTRY)
    result = roi(copy, "verify", "--through", THROUGH)
    assert result.exit_code == 3, result.output
    assert f"GL tie failed: team={team} month={month} spend={spend} gl={spend + 1} diff=-1" in result.output
    # the mart was built before the edit and still says everything ties: the books are what is checked
    assert fetch(copy, "SELECT sum(abs(tie_diff_cents)) FROM fct_ai_spend_team_month")[0][0] == 0


def test_one_cent_in_the_spend_export_is_refused(copy):
    team, month = GL_KEY
    spend = month_spend(copy, team, month)
    edit(copy, "UPDATE raw_ai_spend SET cost_cents = cost_cents + 1 WHERE rowid = "
               "(SELECT min(rowid) FROM raw_ai_spend WHERE team_id = ? AND strftime(usage_date, '%Y-%m') = ?)",
         team, month)
    result = roi(copy, "verify", "--through", THROUGH)
    assert result.exit_code == 3, result.output
    assert f"GL tie failed: team={team} month={month} spend={spend + 1} gl={spend} diff=1" in result.output


def test_the_unassigned_bucket_is_part_of_the_tie(copy):
    edit(copy, "UPDATE raw_gl_journal SET credit_cents = credit_cents + 1 WHERE entry_id = ?", UNASSIGNED_ENTRY)
    result = roi(copy, "verify", "--through", THROUGH)
    assert result.exit_code == 3, result.output
    assert "GL tie failed: team=unassigned month=2025-09" in result.output


def test_a_stale_mart_is_refused_even_when_the_books_tie(copy):
    team, month = GL_KEY
    edit(copy, "UPDATE fct_ai_spend_team_month SET spend_cents = spend_cents + 1, tie_diff_cents = tie_diff_cents + 1"
               " WHERE team_id = ? AND month = DATE '2025-09-01'", team)
    result = roi(copy, "verify", "--through", THROUGH)
    assert result.exit_code == 3, result.output
    assert f"mart disagrees with the books: team={team} month={month}" in result.output


def test_other_ledger_accounts_do_not_count(copy):
    edit(copy, "UPDATE raw_gl_journal SET debit_cents = debit_cents + 1 WHERE account_code <> '6100'")
    result = roi(copy, "verify", "--through", THROUGH)
    assert result.exit_code == 0, result.output


def test_months_after_the_through_month_are_not_checked(copy):
    edit(copy, "UPDATE raw_gl_journal SET debit_cents = debit_cents + 1 WHERE entry_id = 'gl-support-2026-03-6100'")
    assert roi(copy, "verify", "--through", "2026-02").exit_code == 0
    result = roi(copy, "verify", "--through", THROUGH)
    assert result.exit_code == 3
    assert "team=support month=2026-03" in result.output


def test_every_failing_team_month_is_listed_up_to_a_cap(copy):
    edit(copy, "UPDATE raw_gl_journal SET debit_cents = debit_cents + 1 WHERE account_code = '6100'")
    result = roi(copy, "verify", "--through", THROUGH)
    assert result.exit_code == 3
    assert result.output.count("GL tie failed:") == 10
    assert "... and " in result.output


def site_hashes(work):
    return {
        str(path.relative_to(work / "site")): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted((work / "site").rglob("*"))
        if path.is_file()
    }


def test_the_memo_check_passes_on_untouched_books(built):
    result = roi(built, "memo", "--month", THROUGH, "--check")
    assert result.exit_code == 0, result.output
    assert "memo check:" in result.output


def test_one_cent_in_the_ledger_is_refused_by_the_memo_too(copy):
    team, month = GL_KEY
    assert roi(copy, "memo", "--month", THROUGH).exit_code == 0
    assert (copy / "site" / "index.html").is_file()
    before = site_hashes(copy)
    edit(copy, "UPDATE raw_gl_journal SET debit_cents = debit_cents + 1 WHERE entry_id = ?", GL_ENTRY)
    result = roi(copy, "memo", "--month", THROUGH, "--check")
    assert result.exit_code == 3, result.output
    assert team in result.output and month in result.output
    assert site_hashes(copy) == before


def test_verify_does_not_write_the_warehouse(built):
    path = built / "warehouse" / "roi.duckdb"
    before = path.read_bytes()
    assert roi(built, "verify", "--through", THROUGH).exit_code == 0
    assert path.read_bytes() == before


def test_no_warehouse_is_a_usage_error(tmp_path):
    result = roi(tmp_path, "verify", "--through", THROUGH)
    assert result.exit_code == 2
    assert result.output.startswith("error[2]:")


@pytest.mark.parametrize("through", ["March", "2026-13", "2026-04", "2020-01"])
def test_bad_or_unknown_month_is_a_usage_error(built, through):
    assert roi(built, "verify", "--through", through).exit_code == 2
