"""The analytic standard error in `roi.design` against a Monte Carlo of `roi.gen.panel`, and
against the estimator's own variance on the Beta Co design.

The Monte Carlo uses a ten-line ATT written here, with no import from `roi.estimator`.
"""

import math
from pathlib import Path

import numpy as np
import pytest

from roi import design, estimator
from roi.gen import config, panel

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "tests" / "fixtures" / "gen_fixture.yaml"
SHIPPED = ROOT / "config" / "gen.yaml"
DRAWS = 2000
ESTIMATOR_SEEDS = 300
TOLERANCE = 0.10
MARCH, APRIL = "2026-03", "2026-04"
MEASURED_MARCH = ("support", "sales_development", "product_engineering", "marketing", "finance_operations")
CHANGING_CONTROL_SETS = ("sales_development", "legal_operations")
# (team, through) pairs whose standard error is checked.
TARGETS = [(team, MARCH) for team in MEASURED_MARCH] + [("legal_operations", APRIL)]


@pytest.fixture(scope="module", params=[FIXTURE, SHIPPED], ids=["fixture", "shipped"])
def cfg(request):
    return config.load(request.param)


def log_outcomes(sim):
    """Y = direction x ln(outcome) per team, indexed by month position."""
    return {tp.spec.team_id: tp.spec.direction * np.log(tp.outcome) for tp in sim.teams}


def att(cfg, y, months, team, through):
    """Equal-weight mean over the post cells of the backcast difference delta_t - delta_{g-1}."""
    g = next(s.rollout_month for s in cfg.teams if s.team_id == team)
    base = months.index(g) - 1
    out = []
    for t in months_range(months, g, through):
        horizon = max(months[t], months[base])
        controls = [s.team_id for s in cfg.teams
                    if s.team_id != team and (s.rollout_month is None or s.rollout_month > horizon)]
        if controls:
            c = np.mean([y[k] for k in controls], axis=0)
            out.append((y[team][t] - c[t]) - (y[team][base] - c[base]))
    return float(np.mean(out))


def months_range(months, first, last):
    return [k for k, m in enumerate(months) if first <= m <= last]


def test_standard_error_matches_monte_carlo(cfg):
    sims = [panel.simulate(cfg, seed) for seed in range(1, DRAWS + 1)]
    months = list(sims[0].months)
    ys = [log_outcomes(sim) for sim in sims]
    for team, through in TARGETS:
        draws = [att(cfg, y, months, team, through) for y in ys]
        planted = design.planted_team_effect(cfg, team, through)
        empirical = float(np.std(draws, ddof=1))
        analytic = design.team_effect_se(cfg, team, through)
        assert empirical == pytest.approx(analytic, rel=TOLERANCE), (team, through)
        assert float(np.mean(draws)) == pytest.approx(planted, abs=5 * analytic / math.sqrt(DRAWS)), (team, through)


def estimator_panel(sim, through):
    y, outcome, rollout = {}, {}, {}
    keep = [k for k, m in enumerate(sim.months) if m <= through]
    for tp in sim.teams:
        team = tp.spec.team_id
        y[team] = {sim.months[k]: float(tp.spec.direction * math.log(tp.outcome[k])) for k in keep}
        outcome[team] = {sim.months[k]: float(tp.outcome[k]) for k in keep}
        rollout[team] = tp.spec.rollout_month
    return estimator.Panel(y, rollout, outcome)


def test_estimator_variance_tracks_design_variance_on_the_shipped_design():
    """The estimator's cell variance is the sample variance of one-period changes of the backcast,
    and consecutive changes are negatively correlated, so with n_pre - 1 changes it overstates a
    cell's variance by a factor 1 + 1/(n_pre - 1) in expectation (the covariances, taken over the
    levels, are unbiased). The estimator therefore never sits below the design and stays within
    that factor, with 10% allowed for the finite number of draws."""
    cfg = config.load(SHIPPED)
    sums = {target: 0.0 for target in TARGETS}
    for seed in range(1, ESTIMATOR_SEEDS + 1):
        sim = panel.simulate(cfg, seed)
        for through in (MARCH, APRIL):
            results = estimator.estimate(estimator_panel(sim, through), through)
            for team, t in TARGETS:
                if t == through:
                    sums[(team, t)] += results[team].analysis.var
    for (team, through), total in sums.items():
        d = design.team_design(cfg, team, through)
        ratio = total / ESTIMATOR_SEEDS / d.se**2
        assert 1 - TOLERANCE <= ratio <= (1 + TOLERANCE) * (1 + 1 / (d.n_pre - 1)), (team, through, ratio)


def test_constants_are_the_estimators():
    assert (design.MIN_PRE, design.MIN_POST) == (estimator.MIN_PRE, estimator.MIN_POST)


def test_sigma_of_a_team_with_changing_controls_has_the_intersection_term(cfg):
    noise = {s.team_id: s.noise_sd**2 for s in cfg.teams}
    for team, through in (("sales_development", MARCH), ("legal_operations", APRIL)):
        months = design.cells(cfg, team, through)
        sets = [set(design.control_set(cfg, team, t)) for t in months]
        if team == "sales_development":
            assert len({frozenset(s) for s in sets}) > 1
        sig = design.sigma(cfg, team, through)
        own = noise[team]
        for a in range(len(months)):
            for b in range(len(months)):
                shared = sum(noise[c] for c in sets[a] & sets[b]) / (len(sets[a]) * len(sets[b]))
                assert sig[a, b] == pytest.approx((2.0 if a == b else 1.0) * (own + shared))
        assert np.allclose(sig, sig.T)
        assert np.linalg.eigvalsh(sig).min() > 0.0


def test_control_set_follows_the_estimators_rule(cfg):
    # Sales Development rolls out 2025-08: People Operations never, and every team whose rollout
    # is after max(t, 2025-07). Legal Operations (2026-02) has only the never-treated team.
    sd = [design.control_set(cfg, "sales_development", t) for t in ("2025-08", "2025-09", "2025-12", MARCH)]
    assert all("people_operations" in s and "sales_development" not in s for s in sd)
    assert len(set(sd)) > 1
    assert design.control_set(cfg, "legal_operations", APRIL) == ("people_operations",)


def test_design_report_is_consistent(cfg):
    out = design.report(cfg, MARCH)
    assert tuple(sorted(out)) == tuple(sorted(MEASURED_MARCH))
    support = out["support"]
    assert (support.n_pre, support.n_post, support.df) == (3, 9, 1)
    assert support.se == pytest.approx(design.team_effect_se(cfg, "support", MARCH))
    assert support.ratio == pytest.approx(support.planted / support.se)
    assert support.sigma.shape == (9, 9)
    assert "legal_operations" in design.report(cfg, APRIL)
    assert "legal_operations" not in out


def test_t_multiplier_and_degrees_of_freedom():
    assert design.t_multiplier(1) == pytest.approx(12.706, abs=5e-4)
    assert design.df(config.load(FIXTURE), "support", MARCH) == 1


def test_a_team_without_a_window_raises(cfg):
    with pytest.raises(ValueError):
        design.team_design(cfg, "platform_engineering", MARCH)  # no pre period
    with pytest.raises(ValueError):
        design.team_design(cfg, "legal_operations", MARCH)  # two post months
    with pytest.raises(ValueError):
        design.team_design(cfg, "people_operations", MARCH)  # the control
    with pytest.raises(ValueError):
        design.sigma(cfg, "platform_engineering", MARCH)
