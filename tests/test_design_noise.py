"""The noise rule in `roi.design.solve_noise` against the shipped generator config, and the
delta-method standard error of the dose-response curve's marginal return."""

import dataclasses
import math
import statistics
from pathlib import Path

import pytest

from roi import design
from roi.gen import config

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "tests" / "fixtures" / "gen_fixture.yaml"
SHIPPED = ROOT / "config" / "gen.yaml"
TARGET = 3.0
RATIO_TOLERANCE = 1e-3
CONFIG_TOLERANCE = 0.02
TARGETS_BY_TEAM = {
    **{team: "last" for team in design.MEASURED_AT_LAST},
    **{team: "incoming" for team in design.MEASURED_AT_INCOMING},
}


@pytest.fixture(scope="module", params=[FIXTURE, SHIPPED], ids=["fixture", "shipped"])
def cfg(request):
    return config.load(request.param)


@pytest.fixture(scope="module")
def shipped():
    return config.load(SHIPPED)


def through(cfg, team):
    return cfg.last_month if TARGETS_BY_TEAM[team] == "last" else cfg.incoming_month


def with_noise(cfg, sds):
    return dataclasses.replace(cfg, teams=tuple(dataclasses.replace(s, noise_sd=sds[s.team_id]) for s in cfg.teams))


def test_solver_converges_and_hits_the_target_for_every_team_that_can_reach_it(cfg):
    solved = design.solve_noise(cfg)
    assert set(solved) == {s.team_id for s in cfg.teams}
    solved_cfg = with_noise(cfg, solved)
    for team in TARGETS_BY_TEAM:
        ratio = design.team_design(solved_cfg, team, through(cfg, team)).ratio
        if solved[team] > design.NOISE_FLOOR:
            assert ratio == pytest.approx(TARGET, abs=RATIO_TOLERANCE), team
        else:  # the controls' noise alone is above the target: the team sits at the floor
            assert ratio < TARGET, team


def test_every_team_on_the_shipped_config_reaches_the_target(shipped):
    solved = design.solve_noise(shipped)
    assert min(solved.values()) > design.NOISE_FLOOR
    solved_cfg = with_noise(shipped, solved)
    for team in TARGETS_BY_TEAM:
        ratio = design.team_design(solved_cfg, team, through(shipped, team)).ratio
        assert ratio == pytest.approx(TARGET, abs=RATIO_TOLERANCE), team


def test_control_and_unmeasured_team_take_the_median_of_the_five(cfg):
    solved = design.solve_noise(cfg)
    median = statistics.median(solved[t] for t in design.MEASURED_AT_LAST)
    assert solved["people_operations"] == solved["platform_engineering"] == median


def test_solver_raises_when_it_does_not_converge(cfg):
    with pytest.raises(RuntimeError, match="did not converge"):
        design.solve_noise(cfg, tol=0.0, max_iter=3)


def test_the_shipped_noise_column_is_within_two_percent_of_the_solution(shipped):
    solved = design.solve_noise(shipped)
    for spec in shipped.teams:
        assert spec.noise_sd == pytest.approx(solved[spec.team_id], rel=CONFIG_TOLERANCE), spec.team_id


def test_marginal_se_is_finite_and_positive_for_each_measured_team(shipped, capsys):
    with capsys.disabled():
        print()
        for team in design.MEASURED_AT_LAST:
            end = shipped.last_month
            se = design.marginal_se(shipped, team, end)
            assert math.isfinite(se) and se > 0.0, team
            print(f"identified_at_design {team} through {end}: {design.identified_at_design(shipped, team, end)} (marginal_se {se:.3g})")


def test_marginal_se_vanishes_without_noise(shipped):
    quiet = with_noise(shipped, {s.team_id: 1e-9 for s in shipped.teams})
    for team in design.MEASURED_AT_LAST:
        assert design.marginal_se(quiet, team, shipped.last_month) < 1e-6, team
    assert design.identified_at_design(quiet, "support", shipped.last_month)


def test_marginal_se_grows_with_noise(shipped):
    loud = with_noise(shipped, {s.team_id: 2 * s.noise_sd for s in shipped.teams})
    for team in design.MEASURED_AT_LAST:
        base = design.marginal_se(shipped, team, shipped.last_month)
        assert design.marginal_se(loud, team, shipped.last_month) == pytest.approx(2 * base, rel=1e-9), team
