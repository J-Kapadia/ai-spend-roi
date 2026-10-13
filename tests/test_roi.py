"""ROI, its sensitivity and the verdict: hand-worked units, then the build step on the fixture company."""

import math
import shutil
from pathlib import Path

import duckdb
import numpy as np
import pytest
from click.testing import CliRunner

from roi import alloc
from roi import roi as calc
from roi.cli import main
from roi.steps import roi as step
from roi.workdir import RoiError

ROOT = Path(__file__).resolve().parents[1]
GEN_FIXTURE = ROOT / "tests" / "fixtures" / "gen_fixture.yaml"
ROI_FIXTURE = ROOT / "tests" / "fixtures" / "roi_fixture.yaml"
SHIPPED = ROOT / "config" / "roi.yaml"
THROUGH = "2026-03"


def roi(work, *args):
    return CliRunner().invoke(main, ["--work-dir", str(work), *args])


def config(**overrides) -> calc.RoiConfig:
    base = dict(teams={}, multipliers=(0.5, 1.0, 2.0), survive_share=0.9, path=Path("x.yaml"))
    return calc.RoiConfig(**{**base, **overrides})


# ---- the formulas, worked by hand -------------------------------------------------------------

def test_unit_lift_of_a_higher_is_better_metric():
    # an effect of ln 1.2 on a baseline of 10 is a lift of 10 x 0.2
    assert calc.unit_lift(math.log(1.2), 10.0, 1) == pytest.approx(2.0)


def test_unit_lift_of_a_lower_is_better_metric():
    # the signed effect ln(1/0.75) means the level fell to 75% of its baseline of 8: a reduction of 2
    assert calc.unit_lift(-math.log(0.75), 8.0, -1) == pytest.approx(2.0)


def test_a_negative_effect_is_a_negative_lift_never_clipped():
    assert calc.unit_lift(math.log(0.9), 10.0, 1) == pytest.approx(-1.0)
    assert calc.unit_lift(-math.log(1.25), 8.0, -1) == pytest.approx(-2.0)


def test_exposure_depends_on_the_kind():
    assert calc.exposure(calc.ADDITIVE, (4, 4, 5)) == 13.0  # headcount-months
    assert calc.exposure(calc.TEAM_LEVEL, (4, 4, 5)) == 3.0  # months


def test_benefit_is_rounded_half_even_once_in_cents():
    assert calc.benefit_cents(1.0, 1.0, 0.125) == 12  # 12.5 cents
    assert calc.benefit_cents(1.0, 1.0, 0.375) == 38  # 37.5 cents
    assert calc.benefit_cents(1.0, 1.0, 0.625) == 62  # 62.5 cents
    assert calc.benefit_cents(2.0, 13.0, 1.255) == 3263


def test_survives_share_counts_draws_that_pay_back_the_spend():
    values = np.arange(1.0, 11.0)  # one unit of lift pays v x 100 cents; the spend is 500 cents
    assert calc.survives_share(values, 1.0, 500) == 0.5  # 6..10 beat 5, and 5 only ties
    assert calc.survives_share(values, 1.0, 50) == 1.0
    assert calc.survives_share(values, 1.0, 5000) == 0.0
    assert calc.survives_share(values, 1.0, 0) is None  # no spend, no multiple


def test_draws_are_deterministic_bounded_and_independent_of_other_teams():
    a = calc.value_draws("support", 10.0, 30.0)
    assert len(a) == alloc.DRAWS
    assert a.min() >= 10.0 and a.max() <= 30.0
    assert np.array_equal(a, calc.value_draws("support", 10.0, 30.0))
    calc.value_draws("marketing", 1.0, 2.0)  # drawing for another team first changes nothing
    assert np.array_equal(a, calc.value_draws("support", 10.0, 30.0))
    assert not np.array_equal(a, calc.value_draws("marketing", 10.0, 30.0))
    assert a.mean() == pytest.approx(20.0, abs=0.6)


def test_the_draws_are_the_ones_the_allocation_uses():
    unit = alloc.value_draws("support")
    assert np.allclose(calc.value_draws("support", 10.0, 30.0), 10.0 + 20.0 * unit)


def test_a_break_even_in_the_middle_of_the_range_survives_about_half_the_draws():
    draws = calc.value_draws("support", 0.0, 10.0)
    share = calc.survives_share(draws, 1.0, 500)  # break-even value 5
    assert share == pytest.approx(0.5, abs=0.06)


@pytest.mark.parametrize(("lo", "hi", "share", "want"), [
    (1.2, 3.0, 0.95, calc.GOOD),
    (1.2, 3.0, 0.90, calc.GOOD),                    # the 90% rule is "at least"
    (1.2, 3.0, 0.8999, calc.NOT_DISTINGUISHABLE),   # the interval is clear, the value range is not
    (0.9, 3.0, 1.00, calc.NOT_DISTINGUISHABLE),     # the interval spans break-even
    (0.2, 0.9, 0.00, calc.POOR),
    (0.2, 0.9, 0.95, calc.POOR),
    (1.0, 3.0, 1.00, calc.NOT_DISTINGUISHABLE),     # a lower bound of exactly 1 is not above break-even
    (None, None, None, calc.NOT_DISTINGUISHABLE),   # no spend
])
def test_verdict_has_one_label_per_case(lo, hi, share, want):
    assert calc.verdict(lo, hi, share, 0.90) == want


# ---- one team, worked by hand -----------------------------------------------------------------

def team_input(**kw) -> calc.TeamInput:
    base = dict(team_id="t", kind="additive", direction=1, effect=math.log(1.2), ci_lo=math.log(1.1),
                ci_hi=math.log(1.3), baseline=10.0, headcounts=(5, 5, 5, 5), spend_cents=100_000)
    return calc.TeamInput(**{**base, **kw})


def test_a_team_worked_by_hand():
    # lift 2.0 per head per month (1.0 and 3.0 at the interval ends) x 20 headcount-months = 40 units
    # (20 and 60); at $100 per unit that is 400,000 cents (200,000 and 600,000) against 100,000 spent.
    r = calc.compute_team(team_input(), calc.Value(50.0, 100.0, 150.0), config())
    assert r.benefit_cents == 400_000
    assert r.spend_cents == 100_000
    assert r.roi_multiple == pytest.approx(4.0)
    assert (r.roi_lo, r.roi_hi) == (pytest.approx(2.0), pytest.approx(6.0))
    assert r.breakeven_value_cents == 2500  # 100,000 cents / 40 units
    assert dict(r.grid) == {0.5: pytest.approx(2.0), 1.0: pytest.approx(4.0), 2.0: pytest.approx(8.0)}
    assert r.roi_survives_share == 1.0  # even $50 per unit pays 200,000 cents
    assert r.verdict == calc.GOOD
    assert r.baseline_outcome == 10.0


def test_a_team_level_team_is_scaled_by_months_not_headcount():
    # baseline 8 days falling to 75%: 2 days per month, 4 months, $300 per day-month: 2 x 4 x 300 = 2,400 USD
    inp = team_input(kind="team_level", direction=-1, effect=-math.log(0.75), ci_lo=0.1, ci_hi=0.5, baseline=8.0,
                     headcounts=(40, 40, 40, 40), spend_cents=120_000)
    r = calc.compute_team(inp, calc.Value(200.0, 300.0, 400.0), config())
    assert r.benefit_cents == 240_000
    assert r.roi_multiple == pytest.approx(2.0)
    assert r.breakeven_value_cents == 15_000  # 120,000 cents / 8 day-months of lift = 150 USD per day-month


def test_a_wide_value_range_can_fail_a_clear_interval():
    # the interval is above break-even at the base value, but a draw below $25 does not pay: 40 units x v x 100 < 100,000
    r = calc.compute_team(team_input(), calc.Value(0.0, 100.0, 100.0), config())
    assert r.roi_lo > 1.0
    assert r.roi_survives_share == pytest.approx(0.75, abs=0.05)
    assert r.verdict == calc.NOT_DISTINGUISHABLE


def test_a_clearly_negative_return_is_poor():
    r = calc.compute_team(team_input(spend_cents=1_000_000), calc.Value(50.0, 100.0, 150.0), config())
    assert r.roi_hi < 1.0  # 600,000 / 1,000,000
    assert r.verdict == calc.POOR


def test_a_negative_effect_gives_a_negative_multiple_and_no_breakeven():
    r = calc.compute_team(team_input(effect=math.log(0.9), ci_lo=math.log(0.8), ci_hi=math.log(1.0)),
                          calc.Value(50.0, 100.0, 150.0), config())
    assert r.benefit_cents < 0 and r.roi_multiple < 0
    assert r.breakeven_value_cents is None
    assert r.roi_survives_share == 0.0
    assert r.verdict == calc.POOR


def test_no_spend_means_no_multiple():
    r = calc.compute_team(team_input(spend_cents=0), calc.Value(50.0, 100.0, 150.0), config())
    assert r.benefit_cents == 400_000
    assert r.roi_multiple is None and r.roi_lo is None and r.roi_hi is None and r.roi_survives_share is None
    assert all(m is None for _, m in r.grid)
    assert r.verdict == calc.NOT_DISTINGUISHABLE


# ---- the config -------------------------------------------------------------------------------

def test_the_shipped_config_has_the_structure_and_no_values():
    cfg = calc.load_config(SHIPPED)
    assert set(cfg.teams) == {
        "platform_engineering", "support", "sales_development", "product_engineering", "marketing",
        "finance_operations", "legal_operations", "people_operations",
    }
    assert {t for t, c in cfg.teams.items() if c.kind == calc.TEAM_LEVEL} == {"finance_operations"}
    assert all(c.value is None for c in cfg.teams.values())
    assert cfg.survive_share == 0.90
    assert cfg.multipliers == (0.5, 0.75, 1.0, 1.25, 1.5)


def test_the_fixture_config_matches_the_shipped_structure():
    shipped, fixture = calc.load_config(SHIPPED), calc.load_config(ROI_FIXTURE)
    assert {t: c.kind for t, c in shipped.teams.items()} == {t: c.kind for t, c in fixture.teams.items()}
    assert all(c.value is not None and c.value.low < c.value.base < c.value.high for c in fixture.teams.values())


def test_the_allocation_and_agenda_read_the_same_values():
    mine, theirs = calc.load_config(ROI_FIXTURE), alloc.load_config(ROI_FIXTURE)
    assert set(theirs.value_per_unit) == set(mine.teams)
    for team, cfg in mine.teams.items():
        assert theirs.value_per_unit[team] == (cfg.value.low, cfg.value.base, cfg.value.high)
    assert theirs.memo_budget_usd == 5000 and theirs.rho == 0.05


@pytest.mark.parametrize("text", [
    "value_per_unit: []",
    "teams: {a: {kind: additive}}",
    "value_per_unit:\n  a: {kind: sideways}",
    "value_per_unit:\n  a: {kind: additive, low: 5, base: 2, high: 9}",
    "value_per_unit:\n  a: {kind: additive, low: x, base: 2, high: 9}",
    "sensitivity: {survive_share: 0}\nvalue_per_unit: {a: {kind: additive}}",
    "value_per_unit: {a: [",
])
def test_a_malformed_config_is_a_usage_error(tmp_path, text):
    path = tmp_path / "roi.yaml"
    path.write_text(text)
    with pytest.raises(RoiError) as e:
        calc.load_config(path)
    assert e.value.exit_code == 2


def test_a_missing_config_is_a_usage_error(tmp_path):
    with pytest.raises(RoiError) as e:
        calc.load_config(tmp_path / "nope.yaml")
    assert e.value.exit_code == 2


def test_a_config_without_kinds_is_read_and_the_books_decide(tmp_path):
    path = tmp_path / "roi.yaml"
    path.write_text("value_per_unit:\n  a: {low: 1, base: 2, high: 3}")
    cfg = calc.load_config(path)
    assert cfg.teams["a"].kind is None and cfg.teams["a"].value == calc.Value(1.0, 2.0, 3.0)
    team_rows, _, _ = step.rows(THROUGH, [team_input(team_id="a", kind="team_level")], cfg)
    assert [r[1] for r in team_rows] == ["a"]


def test_partly_entered_values_count_as_not_entered(tmp_path):
    path = tmp_path / "roi.yaml"
    path.write_text("value_per_unit:\n  a: {kind: additive, low: 1, base: null, high: 3}")
    assert calc.load_config(path).teams["a"].value is None


# ---- the step's rules --------------------------------------------------------------------------

def test_a_listed_measured_team_without_values_stops_the_build_naming_the_gate():
    cfg = calc.load_config(SHIPPED)
    inputs = [team_input(team_id="support")]
    with pytest.raises(RoiError) as e:
        step.rows(THROUGH, inputs, cfg)
    assert e.value.exit_code == 4
    assert "gate G1" in e.value.message and "support" in e.value.message


def test_a_team_the_config_does_not_list_gets_no_row():
    team_rows, sens_rows, unlisted = step.rows(THROUGH, [team_input(team_id="beta")], calc.load_config(SHIPPED))
    assert team_rows == [] and sens_rows == [] and unlisted == ["beta"]


def test_a_kind_that_disagrees_with_the_books_is_a_usage_error():
    cfg = calc.load_config(ROI_FIXTURE)
    with pytest.raises(RoiError) as e:
        step.rows(THROUGH, [team_input(team_id="finance_operations", kind="additive")], cfg)
    assert e.value.exit_code == 2


def test_the_config_beside_a_generator_config():
    assert step.sibling(Path("config/gen.yaml")) == Path("config/roi.yaml")
    assert step.sibling(Path("tests/fixtures/gen_fixture.yaml")) == Path("tests/fixtures/roi_fixture.yaml")
    assert step.sibling(Path("tests/other.yaml")) is None
    assert step.sibling(Path("tests/generator.yaml")) is None


# ---- the step on the fixture company -----------------------------------------------------------

@pytest.fixture(scope="module")
def built(tmp_path_factory):
    work = tmp_path_factory.mktemp("built")
    result = roi(work, "build", "--config", str(GEN_FIXTURE))
    assert result.exit_code == 0, result.output
    return work


def fetch(work, sql, *params):
    con = duckdb.connect(str(work / "warehouse" / "roi.duckdb"), read_only=True)
    try:
        return con.execute(sql, list(params)).fetchall()
    finally:
        con.close()


def test_only_measured_teams_have_roi_rows(built):
    measured = {t for (t,) in fetch(built, "SELECT team_id FROM est_team_effect WHERE status = 'measured'")}
    assert len(measured) == 5
    assert {t for (t,) in fetch(built, "SELECT team_id FROM roi_team")} == measured
    assert {t for (t,) in fetch(built, "SELECT team_id FROM roi_sensitivity")} == measured
    assert fetch(built, "SELECT count(*) FROM roi_sensitivity")[0][0] == 5 * 5  # five teams, five multipliers


def test_roi_rows_equal_a_recomputation_from_the_estimates(built):
    fixture = calc.load_config(ROI_FIXTURE)
    for team, kind, direction, effect, lo, hi, baseline in fetch(
        built,
        "SELECT e.team_id, d.kind, d.direction, e.effect, e.ci_lo, e.ci_hi, e.baseline_outcome"
        " FROM est_team_effect e JOIN dim_team d USING (team_id) WHERE e.status = 'measured'",
    ):
        months = [m for (m,) in fetch(built, "SELECT t FROM est_att_gt WHERE team_id = ? ORDER BY t", team)]
        heads = [fetch(built, "SELECT headcount FROM fct_team_month WHERE team_id = ? AND month = ?", team, m)[0][0]
                 for m in months]
        spend = sum(fetch(built, "SELECT spend_cents FROM fct_ai_spend_team_month WHERE team_id = ? AND month = ?",
                          team, m)[0][0] for m in months)
        expo = sum(heads) if kind == "additive" else len(months)
        value = fixture.teams[team].value.base
        lift = baseline * (math.exp(effect) - 1) if direction == 1 else baseline * (1 - math.exp(-effect))
        want = round(lift * expo * value * 100)
        got = fetch(built, "SELECT benefit_cents, spend_cents, roi_multiple, baseline_outcome FROM roi_team"
                           " WHERE team_id = ?", team)[0]
        assert got[0] == want, team
        assert got[1] == spend, team
        assert got[2] == pytest.approx(want / spend), team
        assert got[3] == baseline
        assert months[0] == fetch(built, "SELECT rollout_month FROM fct_team_month WHERE team_id = ? LIMIT 1", team)[0][0]


def test_the_interval_is_ordered_and_the_verdict_follows_it(built):
    for team, lo, mid, hi, verdict in fetch(
        built, "SELECT team_id, roi_lo, roi_multiple, roi_hi, roi_verdict FROM roi_team"
    ):
        assert lo <= mid <= hi, team
        assert verdict in calc.VERDICTS
        if verdict == calc.GOOD:
            assert lo > 1.0, team
            (share,) = fetch(built, "SELECT DISTINCT roi_survives_share FROM roi_sensitivity WHERE team_id = ?", team)[0]
            assert share >= 0.90, team
        if verdict == calc.POOR:
            assert hi < 1.0, team


def test_the_grid_is_linear_in_the_value_and_repeats_the_share(built):
    for team, base in fetch(built, "SELECT team_id, roi_multiple FROM roi_team"):
        grid = fetch(built, "SELECT value_multiplier, roi_multiple, roi_survives_share FROM roi_sensitivity"
                            " WHERE team_id = ? ORDER BY value_multiplier", team)
        assert [m for m, _, _ in grid] == [0.5, 0.75, 1.0, 1.25, 1.5]
        assert dict((m, r) for m, r, _ in grid)[1.0] == base
        shares = {s for _, _, s in grid}
        assert len(shares) == 1 and 0.0 <= shares.pop() <= 1.0
        for m, r, _ in grid:
            assert r == pytest.approx(base * m, rel=1e-3)


def test_the_roi_step_runs_between_the_estimator_and_provenance():
    from roi.steps import discover
    names = [s.NAME for s in discover()]
    assert names.index("estimate") < names.index("roi") < names.index("provenance")


def test_a_build_without_values_exits_4_naming_the_gate(tmp_path):
    gen = tmp_path / "elsewhere.yaml"  # no config of the same name beside it: the shipped, empty one applies
    shutil.copy(GEN_FIXTURE, gen)
    result = roi(tmp_path / "w", "build", "--config", str(gen))
    assert result.exit_code == 4, result.output
    assert "gate G1" in result.output
    assert not (tmp_path / "w" / "warehouse" / "roi.duckdb").exists()
    assert not (tmp_path / "w" / "warehouse" / "roi.next.duckdb").exists()


def test_the_work_dir_config_wins_and_a_different_value_changes_the_benefit(built, tmp_path):
    work = tmp_path / "w"
    (work / "config").mkdir(parents=True)
    text = ROI_FIXTURE.read_text().replace(
        "support: {kind: additive, low: 6, base: 12, high: 24}",
        "support: {kind: additive, low: 12, base: 24, high: 48}")
    assert text != ROI_FIXTURE.read_text()
    (work / "config" / "roi.yaml").write_text(text)
    gen = tmp_path / "elsewhere.yaml"
    shutil.copy(GEN_FIXTURE, gen)
    assert roi(work, "build", "--config", str(gen)).exit_code == 0
    doubled = fetch(work, "SELECT benefit_cents FROM roi_team WHERE team_id = 'support'")[0][0]
    once = fetch(built, "SELECT benefit_cents FROM roi_team WHERE team_id = 'support'")[0][0]
    assert abs(doubled - 2 * once) <= 1  # each is rounded to a cent on its own
