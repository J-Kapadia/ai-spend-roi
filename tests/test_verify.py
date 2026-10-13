"""`roi verify`: Scenario 2 on a fixture build, then each way a group can fail, and the control report."""

import json
import shutil
from pathlib import Path

import duckdb
import numpy as np
import pytest
from click.testing import CliRunner

from roi import estimator, verify_books
from roi.cli import main

FIXTURE = Path(__file__).parent / "fixtures" / "gen_fixture.yaml"
ROI_FIXTURE = Path(__file__).parent / "fixtures" / "roi_fixture.yaml"
THROUGH = "2026-03"


def roi(work, *args):
    return CliRunner().invoke(main, ["--work-dir", str(work), *args])


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    work = tmp_path_factory.mktemp("built")
    result = roi(work, "build", "--config", str(FIXTURE))
    assert result.exit_code == 0, result.output
    return work


@pytest.fixture
def copy(built, tmp_path):
    shutil.copytree(built, tmp_path / "w", symlinks=True)
    return tmp_path / "w"


def edit(work, *statements):
    con = duckdb.connect(str(work / "warehouse" / "roi.duckdb"))
    try:
        for sql in statements:
            con.execute(sql)
    finally:
        con.close()


def verify(work, through=THROUGH):
    return roi(work, "verify", "--through", through)


# ---- Scenario 2 --------------------------------------------------------------------------------

def test_scenario_2_all_three_groups_pass(built):
    result = verify(built)
    assert result.exit_code == 0, result.output
    out = result.output
    assert "(a) GL tie: ok" in out
    assert "(b) interval coverage: ok" in out and "5 of 5 measured teams" in out
    assert "(c) unmeasured discipline: ok" in out
    assert "measured (5): finance_operations, marketing, product_engineering, sales_development, support" in out
    assert "unmeasured (2): legal_operations (too_recent), platform_engineering (no_pre_period)" in out
    assert "control (1): people_operations" in out
    assert "checked: est_team_effect, est_att_gt, roi_team, roi_sensitivity" in out


def test_the_groups_run_in_order_and_the_report_names_an_agenda_written_empty(built):
    out = verify(built).output
    assert out.index("(a)") < out.index("(b)") < out.index("(c)") < out.index("leave-one-control-out")
    assert "written empty, no config/roi.yaml in the work dir: agenda_item" in out  # the build had no config for it


def test_scenario_2_with_the_curve_allocation_and_agenda_built(tmp_path):
    work = tmp_path / "w"
    (work / "config").mkdir(parents=True)
    shutil.copy(ROI_FIXTURE, work / "config" / "roi.yaml")
    assert roi(work, "build", "--config", str(FIXTURE)).exit_code == 0
    result = verify(work)
    assert result.exit_code == 0, result.output
    checked = result.output.split("checked:")[1].splitlines()[0]
    assert "fit_curve" in checked and "alloc_result" in checked and "agenda_item" in checked
    assert "written empty" not in result.output
    rows = duckdb.connect(str(work / "warehouse" / "roi.duckdb"), read_only=True).execute(
        "SELECT team_id FROM agenda_item ORDER BY team_id").fetchall()
    assert [t for (t,) in rows] == ["legal_operations", "platform_engineering"]


def test_an_empty_agenda_fails_when_the_work_dir_has_a_config(copy):
    (copy / "config").mkdir()
    shutil.copy(ROI_FIXTURE, copy / "config" / "roi.yaml")
    result = verify(copy)
    assert result.exit_code == 1, result.output
    assert "team=platform_engineering is unmeasured and has no item in agenda_item" in result.output


def test_verify_does_not_write_the_warehouse(built):
    path = built / "warehouse" / "roi.duckdb"
    before = path.read_bytes()
    assert verify(built).exit_code == 0
    assert path.read_bytes() == before


def test_an_earlier_month_runs_the_tie_and_says_the_rest_was_not_run(built):
    result = verify(built, "2026-02")
    assert result.exit_code == 0, result.output
    assert "(a) GL tie: ok" in result.output
    assert "(b) interval coverage: not run" in result.output
    assert "(c) unmeasured discipline: not run" in result.output


def test_a_tie_failure_comes_first_and_exits_3(copy):
    edit(copy, "UPDATE raw_gl_journal SET debit_cents = debit_cents + 1 WHERE entry_id = 'gl-support-2025-09-6100'")
    result = verify(copy)
    assert result.exit_code == 3
    assert "GL tie failed: team=support month=2025-09" in result.output
    assert "(b)" not in result.output


# ---- group (b) ---------------------------------------------------------------------------------

def shift_truth(work, team, delta):
    path = work / "truth" / "truth.json"
    truth = json.loads(path.read_text())
    effects = truth["teams"][team]["planted_effect_by_month"]
    truth["teams"][team]["planted_effect_by_month"] = {m: v + delta for m, v in effects.items()}
    path.write_text(json.dumps(truth))


def test_a_planted_effect_outside_its_interval_exits_1_naming_the_team_and_both_numbers(copy):
    shift_truth(copy, "marketing", 1.0)
    result = verify(copy)
    assert result.exit_code == 1, result.output
    assert "interval coverage failed: team=marketing planted=" in result.output
    assert "interval=[" in result.output and "effect=" in result.output
    assert "(b) interval coverage: FAILED" in result.output
    assert "4 of 5 measured teams" in result.output
    assert "(c) unmeasured discipline: ok" in result.output  # the other groups still report


def test_the_planted_effect_is_the_mean_over_the_cells_of_the_team_effect(built):
    # the fixture's planted effects, worked by hand for one team from its cells' months and the truth file
    truth = json.loads((built / "truth" / "truth.json").read_text())["teams"]["finance_operations"]
    con = duckdb.connect(str(built / "warehouse" / "roi.duckdb"), read_only=True)
    months = [m for (m,) in con.execute(
        "SELECT strftime(t, '%Y-%m') FROM est_att_gt WHERE team_id = 'finance_operations' ORDER BY t").fetchall()]
    con.close()
    want = sum(truth["planted_effect_by_month"][m] for m in months) / len(months)
    assert len(months) == 4
    assert f"planted {want:+.4f}" in verify(built).output


def test_a_missing_truth_file_fails_closed_with_exit_4(copy):
    (copy / "truth" / "truth.json").unlink()
    result = verify(copy)
    assert result.exit_code == 4, result.output
    assert "truth file is missing" in result.output


def test_a_truth_file_that_does_not_cover_a_measured_team_exits_4(copy):
    path = copy / "truth" / "truth.json"
    truth = json.loads(path.read_text())
    del truth["teams"]["support"]
    path.write_text(json.dumps(truth))
    result = verify(copy)
    assert result.exit_code == 4
    assert "team=support" in result.output


# ---- group (c) ---------------------------------------------------------------------------------

def test_an_roi_row_for_an_unmeasured_team_fails(copy):
    edit(copy, "INSERT INTO roi_team SELECT through_month, 'platform_engineering', benefit_cents, spend_cents,"
               " roi_multiple, roi_lo, roi_hi, breakeven_value_cents, baseline_outcome, roi_verdict"
               " FROM roi_team WHERE team_id = 'support'")
    result = verify(copy)
    assert result.exit_code == 1, result.output
    assert "team=platform_engineering is unmeasured and has a row in roi_team" in result.output


def test_a_sensitivity_row_for_the_control_team_fails(copy):
    edit(copy, "INSERT INTO roi_sensitivity SELECT through_month, 'people_operations', value_multiplier,"
               " roi_multiple, roi_survives_share FROM roi_sensitivity WHERE team_id = 'support'")
    result = verify(copy)
    assert result.exit_code == 1
    assert "team=people_operations is control and has a row in roi_sensitivity" in result.output


def test_a_measured_team_without_its_roi_row_fails(copy):
    edit(copy, "DELETE FROM roi_team WHERE team_id = 'support'")
    result = verify(copy)
    assert result.exit_code == 1
    assert "team=support is measured and has no row in roi_team" in result.output


def test_a_reason_that_does_not_match_its_rule_fails(copy):
    edit(copy, "UPDATE est_team_effect SET reason = 'too_recent' WHERE team_id = 'platform_engineering'")
    result = verify(copy)
    assert result.exit_code == 1
    assert "team=platform_engineering status=unmeasured reason=too_recent but the books say unmeasured/no_pre_period" \
        in result.output


def test_a_team_marked_measured_that_the_books_say_has_no_pre_period_fails(copy):
    edit(copy, "UPDATE est_team_effect SET status = 'measured', reason = NULL, effect = 0.1, ci_lo = 0.0, ci_hi = 0.2"
               " WHERE team_id = 'platform_engineering'",
         "INSERT INTO est_att_gt SELECT through_month, 'platform_engineering', g, t, att, se, n_controls"
         " FROM est_att_gt WHERE team_id = 'support'")
    result = verify(copy)
    assert result.exit_code == 1
    assert "team=platform_engineering status=measured" in result.output


def test_a_measured_team_without_cells_exits_3(copy):
    edit(copy, "DELETE FROM est_att_gt WHERE team_id = 'support'")
    result = verify(copy)
    assert result.exit_code == 3, result.output
    assert "team=support is measured and has no group-time cells" in result.output


def test_an_unmeasured_team_that_carries_an_interval_fails(copy):
    edit(copy, "UPDATE est_team_effect SET effect = 0.1, ci_lo = 0.0, ci_hi = 0.2 WHERE team_id = 'legal_operations'")
    result = verify(copy)
    assert result.exit_code == 1
    assert "team=legal_operations is unmeasured and carries an effect or interval" in result.output


def test_group_time_cells_of_an_unmeasured_team_fail(copy):
    edit(copy, "INSERT INTO est_att_gt SELECT through_month, 'legal_operations', g, t, att, se, n_controls"
               " FROM est_att_gt WHERE team_id = 'support' AND t = DATE '2025-09-01'")
    result = verify(copy)
    assert result.exit_code == 1
    assert "team=legal_operations is not measured and has group-time cells" in result.output


def test_a_control_with_a_reason_fails(copy):
    edit(copy, "UPDATE est_team_effect SET status = 'unmeasured', reason = 'no_comparison' WHERE team_id = 'people_operations'")
    result = verify(copy)
    assert result.exit_code == 1
    assert "team=people_operations status=unmeasured" in result.output


def test_a_wrong_pre_period_count_fails(copy):
    edit(copy, "UPDATE est_team_effect SET n_pre = n_pre + 1 WHERE team_id = 'support'")
    result = verify(copy)
    assert result.exit_code == 1
    assert "team=support n_pre=" in result.output


def test_a_measured_team_with_a_significant_pre_trend_fails(copy):
    edit(copy, "UPDATE est_team_effect SET pretrend_testable = true, pretrend_p = 0.01 WHERE team_id = 'support'")
    result = verify(copy)
    assert result.exit_code == 1
    assert "team=support is measured with a significant pre-trend" in result.output


def test_a_missing_result_table_exits_3(copy):
    edit(copy, "DROP TABLE roi_sensitivity")
    result = verify(copy)
    assert result.exit_code == 3, result.output
    assert "no roi_sensitivity" in result.output


def test_a_warehouse_without_estimates_exits_3(copy):
    edit(copy, "DROP TABLE est_team_effect")
    assert verify(copy).exit_code == 3


@pytest.mark.parametrize("table", ["fit_curve", "alloc_result"])
def test_a_later_table_is_checked_once_it_exists(copy, table):
    edit(copy, f"DROP TABLE IF EXISTS {table}", f"CREATE TABLE {table}(through_month VARCHAR, team_id VARCHAR)",
         f"INSERT INTO {table} VALUES ('{THROUGH}', 'support')")
    result = verify(copy)
    assert result.exit_code == 0, result.output
    assert table in result.output.split("checked:")[1].splitlines()[0]
    edit(copy, f"INSERT INTO {table} VALUES ('{THROUGH}', 'legal_operations')")
    result = verify(copy)
    assert result.exit_code == 1
    assert f"team=legal_operations is unmeasured and has a row in {table}" in result.output


def test_an_agenda_needs_an_item_for_each_unmeasured_team_and_no_other(copy):
    edit(copy, "DROP TABLE IF EXISTS agenda_item", "CREATE TABLE agenda_item(through_month DATE, team_id VARCHAR)",
         f"INSERT INTO agenda_item VALUES (DATE '{THROUGH}-01', 'platform_engineering'),"
         f" (DATE '{THROUGH}-01', 'legal_operations')")
    result = verify(copy)
    assert result.exit_code == 0, result.output  # through_month may be a date or a string
    assert "agenda_item" in result.output.split("checked:")[1].splitlines()[0]
    edit(copy, "DELETE FROM agenda_item WHERE team_id = 'legal_operations'")
    result = verify(copy)
    assert result.exit_code == 1
    assert "team=legal_operations is unmeasured and has no item in agenda_item" in result.output
    edit(copy, f"INSERT INTO agenda_item VALUES (DATE '{THROUGH}-01', 'legal_operations'),"
               f" (DATE '{THROUGH}-01', 'support'), (DATE '{THROUGH}-01', 'people_operations')")
    result = verify(copy)
    assert result.exit_code == 1
    assert "team=support is measured and has an item in agenda_item" in result.output
    assert "team=people_operations is control and has an item in agenda_item" in result.output


# ---- leave-one-control-out ---------------------------------------------------------------------

def panel(controls: int) -> estimator.Panel:
    """One team rolled out in month 4 of 7 (three pre, four post) and `controls` never-treated teams."""
    rng = np.random.default_rng(7)
    months = [f"2025-{m:02d}" for m in range(1, 8)]
    y = {"t": {m: float(v) for m, v in zip(months, rng.normal(0, 0.05, 7) + [0, 0, 0, 0.2, 0.2, 0.2, 0.2])}}
    rollout = {"t": "2025-04"}
    for i in range(controls):
        y[f"c{i}"] = {m: float(v) for m, v in zip(months, rng.normal(0, 0.05, 7))}
        rollout[f"c{i}"] = None
    return estimator.Panel(y, rollout, {})


def att_with(p: estimator.Panel, controls: list[str]) -> float:
    """The team effect with only `controls`, worked by hand: mean over t of (T_t - T_3) - mean_c (c_t - c_3)."""
    months = sorted(p.y["t"])
    base = months[2]
    cells = [(p.y["t"][t] - p.y["t"][base]) - np.mean([p.y[c][t] - p.y[c][base] for c in controls])
             for t in months[3:]]
    return float(np.mean(cells))


def test_dropping_each_control_gives_the_effect_on_the_others():
    p = panel(3)
    [row] = verify_books.leave_one_control_out(p, "2025-07")
    assert row.team_id == "t" and row.controls == ("c0", "c1", "c2")
    assert row.effect == pytest.approx(att_with(p, ["c0", "c1", "c2"]))
    assert not row.single_control
    for run in row.runs:
        others = [c for c in row.controls if c != run.dropped]
        assert run.cells == 4
        assert run.effect == pytest.approx(att_with(p, others))


def test_two_controls_are_enough_and_one_is_not():
    [two] = verify_books.leave_one_control_out(panel(2), "2025-07")
    assert [r.dropped for r in two.runs] == ["c0", "c1"]
    [one] = verify_books.leave_one_control_out(panel(1), "2025-07")
    assert one.single_control and one.runs == ()


def test_a_team_that_needs_the_dropped_control_is_not_estimable_without_it():
    # c1 is untreated only through month 5; once it is treated, c0 is the only control and
    # dropping c0 leaves the post months with none
    p = panel(2)
    p = estimator.Panel(p.y, {**p.rollout, "c1": "2025-06"}, p.outcome)
    [row] = verify_books.leave_one_control_out(p, "2025-07")
    assert row.controls == ("c0", "c1")
    by = {r.dropped: r for r in row.runs}
    assert by["c0"].effect is None and by["c0"].cells < estimator.MIN_POST
    assert by["c1"].effect is not None


def test_the_report_in_verify_output_lists_each_measured_team(built):
    out = verify(built).output
    section = out.split("leave-one-control-out")[1]
    for team in ("finance_operations", "marketing", "product_engineering", "sales_development", "support"):
        assert f"  {team}: " in section
    assert "without people_operations" in section
    assert "not a check" in out
