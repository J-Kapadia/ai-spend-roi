"""The dbt models and their tests, on hand-worked books and on deliberately broken ones.

The books below are small enough to work by hand. Team `a` is additive (output per head),
team `b` is the team-level metric (days to close, lower is better) and never uses AI.
Months run 2025-01 to 2025-03.
"""

import csv
import os
import re
import shutil
from datetime import date
from pathlib import Path

import duckdb
import pytest
from click.testing import CliRunner

from roi.cli import main
from roi.steps import BuildContext, dbt, load
from roi.workdir import RoiError, WorkDir

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "gen_fixture.yaml"
DBT_TESTS = ROOT / "dbt" / "tests"

SPEND = "provider_a,model_a1"
BOOKS = {
    "dim_team": [
        "team_id,name,metric_id,unit_label,direction,kind",
        "a,Team A,tickets_resolved,tickets,1,additive",
        "b,Team B,days_to_close,days,-1,team_level",
    ],
    # headcount of a: Jan 2 (a3 leaves on 2025-02-28), Feb 2 (a2 starts mid month, a3 is gone), Mar 3 (a4 starts on the 1st)
    "raw_hr_roster": [
        "employee_id,team_id,start_date,end_date,monthly_payroll_cents",
        "a1,a,2024-12-01,,100000",
        "a2,a,2025-02-15,,100000",
        "a3,a,2024-12-01,2025-02-28,100000",
        "a4,a,2025-03-01,,100000",
        "b1,b,2024-12-01,,100000",
        "b2,b,2024-12-01,,100000",
    ],
    "raw_output": [
        "team_id,month,metric_id,value",
        "a,2025-01-01,tickets_resolved,40",
        "a,2025-02-01,tickets_resolved,44",
        "a,2025-03-01,tickets_resolved,66",
        "b,2025-01-01,days_to_close,10",
        "b,2025-02-01,days_to_close,9",
        "b,2025-03-01,days_to_close,8",
    ],
    "raw_ai_spend": [
        "usage_date,team_id,project_id,user_id,provider,model,input_tokens,output_tokens,cost_cents",
        f"2025-02-03,a,p1,a1,{SPEND},1000,200,1000",
        f"2025-02-20,a,p1,a2,{SPEND},400,100,500",
        f"2025-03-05,a,p1,a1,{SPEND},700,50,600",
        f"2025-03-09,unassigned,p0,u0,{SPEND},100,10,200",
    ],
    # AI usage is account 6100; the 6200 line is another account and must be ignored; net is debit - credit
    "raw_gl_journal": [
        "entry_id,period,account_code,cost_center,debit_cents,credit_cents,memo",
        "gl1,2025-02,6100,a,1500,0,AI usage",
        "gl2,2025-03,6100,a,700,100,AI usage",
        "gl3,2025-03,6100,unassigned,200,0,AI usage",
        "gl4,2025-01,6200,a,9999,0,Other",
    ],
}


def write_books(work: Path, books: dict[str, list[str]] = BOOKS) -> None:
    (work / "data" / "books").mkdir(parents=True, exist_ok=True)
    for table, lines in books.items():
        (work / "data" / "books" / f"{table}.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_models(work: Path, through: str = "2025-03") -> Path:
    """The load and dbt steps of `roi build`, then the swap: the part this module tests."""
    w = WorkDir(work)
    w.warehouse.mkdir(parents=True, exist_ok=True)
    ctx = BuildContext(work=w, db=w.warehouse_next_db, through=through)
    load.run(ctx)
    dbt.run(ctx)
    os.replace(w.warehouse_next_db, w.warehouse_db)
    return w.warehouse_db


def rows(db: Path, sql: str):
    con = duckdb.connect(str(db), read_only=True)
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


def d(y, m):
    return date(y, m, 1)


@pytest.fixture(scope="module")
def db(tmp_path_factory):
    work = tmp_path_factory.mktemp("hand")
    write_books(work)
    return build_models(work)


def test_headcount_counts_employees_on_the_roster_at_month_end(db):
    got = rows(db, "SELECT team_id, month, headcount FROM fct_output_team_month ORDER BY team_id, month")
    assert got == [("a", d(2025, 1), 2), ("a", d(2025, 2), 2), ("a", d(2025, 3), 3),
                   ("b", d(2025, 1), 2), ("b", d(2025, 2), 2), ("b", d(2025, 3), 2)]


def test_spend_beside_the_ledger_with_unassigned_and_other_accounts_left_out(db):
    got = rows(db, "SELECT team_id, month, spend_cents, input_tokens, output_tokens, active_users, gl_cents,"
                   " tie_diff_cents FROM fct_ai_spend_team_month ORDER BY month, team_id")
    assert got == [
        ("a", d(2025, 2), 1500, 1400, 300, 2, 1500, 0),
        ("a", d(2025, 3), 600, 700, 50, 1, 600, 0),  # ledger net is debit 700 less credit 100
        ("unassigned", d(2025, 3), 200, 100, 10, 1, 200, 0),
    ]


def test_outcome_is_per_head_for_additive_and_raw_for_team_level(db):
    got = rows(db, "SELECT team_id, month, output_signed, outcome FROM fct_output_team_month ORDER BY team_id, month")
    assert got == [("a", d(2025, 1), 40.0, 20.0), ("a", d(2025, 2), 44.0, 22.0), ("a", d(2025, 3), 66.0, 22.0),
                   ("b", d(2025, 1), -10.0, 10.0), ("b", d(2025, 2), -9.0, 9.0), ("b", d(2025, 3), -8.0, 8.0)]


def test_panel_carries_log_outcome_signed_by_direction_and_the_rollout_month(db):
    import math
    got = {(t, m): (y, s, p, h, r) for t, m, y, s, p, h, r in rows(
        db, "SELECT team_id, month, y_log, spend_cents, spend_per_head_cents, headcount, rollout_month"
            " FROM fct_team_month")}
    assert len(got) == 6  # the unassigned bucket is not a team of the panel
    assert got[("a", d(2025, 3))] == (pytest.approx(math.log(22)), 600, 200.0, 3, d(2025, 2))
    assert got[("a", d(2025, 1))] == (pytest.approx(math.log(20)), 0, 0.0, 2, d(2025, 2))
    assert got[("b", d(2025, 1))] == (pytest.approx(-math.log(10)), 0, 0.0, 2, None)
    assert got[("b", d(2025, 3))][0] == pytest.approx(-math.log(8))


def test_ai_spend_as_a_share_of_payroll_by_team_and_for_the_company(db):
    got = {(t, m): (s, p, float(sh)) for t, m, s, p, sh in rows(
        db, "SELECT team_id, month, spend_cents, payroll_cents, share FROM fct_ai_share_of_payroll")}
    assert got[("a", d(2025, 2))] == (1500, 200000, pytest.approx(0.0075))
    assert got[("b", d(2025, 2))] == (0, 200000, 0.0)
    # the company row holds the unassigned spend too: (600 + 200) / (300000 + 200000)
    assert got[(None, d(2025, 3))] == (800, 500000, pytest.approx(0.0016))
    assert got[(None, d(2025, 1))] == (0, 400000, 0.0)


def test_marts_are_tables_and_the_settings_are_recorded(db):
    kinds = dict(rows(db, "SELECT table_name, table_type FROM information_schema.tables"))
    for mart in ("fct_ai_spend_team_month", "fct_output_team_month", "fct_ai_share_of_payroll", "fct_team_month"):
        assert kinds[mart] == "BASE TABLE"
    assert rows(db, "SELECT key, value FROM roi_settings") == [("gl_account", "6100")]


def test_verify_books_group_a_passes_on_these_books(db, tmp_path):
    from roi import verify_books
    shutil.copy(db, tmp_path / "x.duckdb")
    con = duckdb.connect(str(tmp_path / "x.duckdb"), read_only=True)
    try:
        report = verify_books.check_gl_tie(con, "2025-03")
    finally:
        con.close()
    assert (report.team_months, report.months) == (3, 2)


# --- every dbt test bites: its SQL returns no rows on the built warehouse, and rows once the
# --- table it guards is corrupted. Generic tests (keys, not null, relationships, accepted
# --- values) are exercised by the failing builds further down.

MUTATIONS = {
    "assert_gl_tie": "UPDATE fct_ai_spend_team_month SET gl_cents = gl_cents + 1, tie_diff_cents = tie_diff_cents - 1"
                     " WHERE team_id = 'a' AND month = DATE '2025-02-01'",
    "assert_company_tie": "UPDATE fct_ai_spend_team_month SET gl_cents = gl_cents + 1"
                          " WHERE team_id = 'a' AND month = DATE '2025-02-01'",
    "assert_unique_spend_key": "INSERT INTO fct_ai_spend_team_month SELECT * FROM fct_ai_spend_team_month LIMIT 1",
    "assert_unique_output_key": "INSERT INTO stg_output SELECT * FROM stg_output LIMIT 1",
    "assert_unique_panel_key": "INSERT INTO fct_team_month SELECT * FROM fct_team_month LIMIT 1",
    "assert_unique_share_key": "INSERT INTO fct_ai_share_of_payroll SELECT * FROM fct_ai_share_of_payroll"
                               " WHERE team_id IS NULL LIMIT 1",
    "assert_headcount_positive": "UPDATE fct_output_team_month SET headcount = 0 WHERE team_id = 'a'",
    "assert_sign_convention": "UPDATE fct_output_team_month SET output_signed = -output_signed WHERE team_id = 'b'",
    "assert_additive_outcome_per_head": "UPDATE fct_output_team_month SET outcome = abs(output_signed)"
                                        " WHERE team_id = 'a'",
    "assert_team_level_outcome_raw": "UPDATE fct_output_team_month SET outcome = outcome / headcount"
                                     " WHERE team_id = 'b'",
    "assert_share_company_ties": "UPDATE fct_ai_share_of_payroll SET spend_cents = spend_cents + 1 WHERE team_id IS NULL",
    "assert_share_in_range": "UPDATE fct_ai_share_of_payroll SET share = -0.1 WHERE team_id = 'a'",
    "assert_rollout_matches_first_spend": "UPDATE fct_team_month SET rollout_month = DATE '2025-01-01'"
                                         " WHERE team_id = 'a'",
}


def test_every_singular_test_has_a_mutation():
    assert {p.stem for p in DBT_TESTS.glob("*.sql")} == set(MUTATIONS)


def test_sign_convention_also_checks_the_log_outcome(tmp_path, db):
    shutil.copy(db, tmp_path / "w.duckdb")
    sql = singular_sql("assert_sign_convention")
    con = duckdb.connect(str(tmp_path / "w.duckdb"))
    try:
        assert con.execute(sql).fetchall() == []
        con.execute("UPDATE fct_team_month SET y_log = -y_log WHERE team_id = 'b'")
        assert con.execute(sql).fetchall() != []
    finally:
        con.close()


def singular_sql(name: str) -> str:
    text = (DBT_TESTS / f"{name}.sql").read_text()
    return re.sub(r"\{\{\s*ref\('(\w+)'\)\s*\}\}", r"\1", text)


@pytest.mark.parametrize("name", sorted(MUTATIONS))
def test_singular_test_passes_clean_and_fails_corrupted(name, db, tmp_path):
    shutil.copy(db, tmp_path / "w.duckdb")
    sql = singular_sql(name)
    con = duckdb.connect(str(tmp_path / "w.duckdb"))
    try:
        assert con.execute(sql).fetchall() == [], "the test fails on clean books"
        con.execute(MUTATIONS[name])
        assert con.execute(sql).fetchall() != [], "the test does not notice the corruption"
    finally:
        con.close()


# --- failing builds: dbt reports the tests that failed, `roi build` exits 3, nothing is published.

def edit_book(work: Path, table: str, fn) -> None:
    path = work / "data" / "books" / f"{table}.csv"
    with path.open(newline="") as f:
        data = list(csv.reader(f))
    header, body = data[0], data[1:]
    fn(header, body)
    with path.open("w", newline="") as f:
        csv.writer(f, lineterminator="\n").writerows([header, *body])


def gen_fixture_books(work: Path) -> None:
    result = CliRunner().invoke(main, ["--work-dir", str(work), "gen", "--config", str(FIXTURE)])
    assert result.exit_code == 0, result.output


def failing_build(work: Path, capsys) -> str:
    with pytest.raises(RoiError) as e:
        build_models(work, through="2026-03")
    assert e.value.exit_code == 3
    assert not (work / "warehouse" / "roi.duckdb").exists()
    return capsys.readouterr().err


def test_a_cent_off_in_the_books_and_a_team_without_headcount_fail_the_mart_tests(tmp_path, capsys):
    gen_fixture_books(tmp_path)

    def one_cent(header, rows_):
        row = next(r for r in rows_ if r[0] == "gl-support-2025-09-6100")
        row[header.index("debit_cents")] = str(int(row[header.index("debit_cents")]) + 1)

    edit_book(tmp_path, "raw_gl_journal", one_cent)
    edit_book(tmp_path, "raw_hr_roster", lambda h, rs: rs.__setitem__(slice(None), [r for r in rs if r[1] != "legal_operations"]))
    out = failing_build(tmp_path, capsys)
    for test in ("assert_gl_tie", "assert_company_tie", "assert_headcount_positive"):
        assert re.search(rf"FAIL \d+ {test}\b", out), out


def test_broken_keys_values_and_references_fail_the_staging_tests(tmp_path, capsys):
    gen_fixture_books(tmp_path)
    edit_book(tmp_path, "raw_hr_roster", lambda h, rs: rs.append(list(rs[0])))  # a duplicated employee
    edit_book(tmp_path, "raw_output", lambda h, rs: rs.append(list(rs[0])))  # a team-month reported twice
    edit_book(tmp_path, "dim_team", lambda h, rs: rs[0].__setitem__(h.index("kind"), "per_head"))
    edit_book(tmp_path, "raw_gl_journal", lambda h, rs: rs[0].__setitem__(h.index("cost_center"), "ghost_team"))
    out = failing_build(tmp_path, capsys)
    for test in ("unique_stg_hr_roster_employee_id", "assert_unique_output_key", "accepted_values_stg_team_kind",
                 "relationships_stg_gl_journal_team_id"):
        assert re.search(rf"FAIL \d+ {test}", out), out
