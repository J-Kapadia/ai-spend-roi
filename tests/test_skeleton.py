"""Walking skeleton: roi build --toy, then roi memo --month 2025-06 --check."""

import shutil

import duckdb
import pytest
from click.testing import CliRunner

from roi.cli import main
from roi.memo import check_site
from roi.workdir import RoiError, WorkDir

MONTH = "2025-06"


def roi(work, *args):
    return CliRunner().invoke(main, ["--work-dir", str(work), *args])


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    work = tmp_path_factory.mktemp("built")
    result = roi(work, "build", "--toy")
    assert result.exit_code == 0, result.output
    return work


@pytest.fixture
def copy(built, tmp_path):
    shutil.copytree(built, tmp_path / "w", symlinks=True)
    return tmp_path / "w"


def test_memo_check_resolves_one_number(built):
    result = roi(built, "memo", "--month", MONTH, "--check")
    assert result.exit_code == 0, result.output
    assert "1 number resolved" in result.output
    assert (built / "site" / "index.html").exists()


def test_state_stays_under_work_dir_and_next_file_is_gone(built):
    assert (built / "warehouse" / "roi.duckdb").exists()
    assert not (built / "warehouse" / "roi.next.duckdb").exists()
    assert (built / "data" / "books" / "raw_ai_spend.csv").exists()
    assert (built / "truth" / "truth.json").exists()


def test_build_is_deterministic(built, tmp_path):
    assert roi(tmp_path, "build", "--toy").exit_code == 0
    for w in (built, tmp_path):
        assert roi(w, "memo", "--month", MONTH).exit_code == 0
    for rel in ["site/index.html", "data/books/raw_output.csv", "data/books/raw_ai_spend.csv", "truth/truth.json"]:
        assert (built / rel).read_bytes() == (tmp_path / rel).read_bytes(), rel


def test_books_tie_to_the_ledger_in_cents(built):
    con = duckdb.connect(str(built / "warehouse" / "roi.duckdb"), read_only=True)
    diff = con.execute(
        "SELECT count(*) FROM (SELECT team_id, strftime(usage_date, '%Y-%m') AS m, sum(cost_cents) AS s"
        " FROM raw_ai_spend GROUP BY 1, 2) a FULL JOIN (SELECT cost_center AS team_id, period AS m,"
        " sum(debit_cents - credit_cents) AS g FROM raw_gl_journal GROUP BY 1, 2) b USING (team_id, m)"
        " WHERE coalesce(a.s, 0) <> coalesce(b.g, 0)"
    ).fetchone()[0]
    assert diff == 0


def test_edited_source_rows_fail_the_check(copy):
    con = duckdb.connect(str(copy / "warehouse" / "roi.duckdb"))
    con.execute("UPDATE fct_team_month SET spend_cents = spend_cents + 1 WHERE team_id = 'alpha' AND month = DATE '2025-06-01'")
    con.close()
    result = roi(copy, "memo", "--month", MONTH, "--check")
    assert result.exit_code == 1
    assert "checksum mismatch" in result.output


def test_hand_edited_number_fails_the_check(copy):
    assert roi(copy, "memo", "--month", MONTH).exit_code == 0
    page = copy / "site" / "index.html"
    page.write_text(page.read_text().replace("$", "$9", 1))
    con = duckdb.connect(str(copy / "warehouse" / "roi.duckdb"), read_only=True)
    with pytest.raises(RoiError, match="records"):
        check_site(WorkDir(copy), MONTH, con)


def test_untagged_number_fails_the_check(copy):
    assert roi(copy, "memo", "--month", MONTH).exit_code == 0
    page = copy / "site" / "index.html"
    page.write_text(page.read_text().replace("</ul>", "</ul><p>We saved 7 hours.</p>"))
    con = duckdb.connect(str(copy / "warehouse" / "roi.duckdb"), read_only=True)
    with pytest.raises(RoiError, match="untagged number"):
        check_site(WorkDir(copy), MONTH, con)


def test_memo_without_a_warehouse_is_a_usage_error(tmp_path):
    result = roi(tmp_path, "memo", "--month", MONTH)
    assert result.exit_code == 2
    assert result.output.startswith("error[2]:")


def test_bad_month_is_a_usage_error(built):
    assert roi(built, "memo", "--month", "June").exit_code == 2
