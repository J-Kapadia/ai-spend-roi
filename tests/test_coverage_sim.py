"""500-seed coverage simulation of the estimator's 95% interval, on `roi.gen.panel`.

Each seed simulates the team-month panel of the generator and estimates every team. For each
measured team the share of seeds in which the interval holds the planted team effect must be at
least 0.90; there is no upper limit (the interval over-covers for teams with one or two pre-period
changes, which only makes it more cautious) and each team's mean interval width is printed. Arms:
the design as is, the common month shock set to zero and to twice its value (all three gated), and
the control team's noise sd doubled (reported, not gated). The pre-trend test's false-flag rate
under the null (the generator plants no drift) and its detection rate on a planted drift are
findings, printed and not gated.

Runs on the fixture design (`tests/fixtures/gen_fixture.yaml`: Beta Co's teams and rollout months,
invented sizes and noise) until the registered Beta Co table exists in `config/gen.yaml`.
"""

import dataclasses
import math
from collections import defaultdict
from pathlib import Path

import numpy as np
import pytest

from roi import estimator
from roi.gen import config, panel

pytestmark = pytest.mark.slow

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "gen_fixture.yaml"
SEEDS = range(1, 501)
MIN_COVERAGE = 0.90
THROUGH_MARCH, THROUGH_APRIL = "2026-03", "2026-04"
MEASURED_MARCH = ("support", "sales_development", "product_engineering", "marketing", "finance_operations")
MEASURED_APRIL = (*MEASURED_MARCH, "legal_operations")
CONTROL = "people_operations"
# The teams whose control sets change from month to month, named in the printed table.
CHANGING_CONTROL_SETS = ("sales_development", "legal_operations")


@pytest.fixture(scope="module")
def cfg():
    return config.load(FIXTURE)


def estimator_panel(sim, through):
    y, outcome, rollout = {}, {}, {}
    for tp in sim.teams:
        team = tp.spec.team_id
        keep = [k for k, m in enumerate(sim.months) if m <= through]
        y[team] = {sim.months[k]: float(tp.spec.direction * math.log(tp.outcome[k])) for k in keep}
        outcome[team] = {sim.months[k]: float(tp.outcome[k]) for k in keep}
        rollout[team] = next((m for k, m in enumerate(sim.months) if tp.spend_cents[k] > 0), None)
    return estimator.Panel(y, rollout, outcome)


def planted_effect(sim, team, through):
    tp = sim.team(team)
    g = tp.spec.rollout_month
    return float(np.mean([tp.effect[k] for k, m in enumerate(sim.months) if g <= m <= through]))


def run_arm(cfg):
    """Per (through, team): list of per-seed tuples
    (status, reason, covered, n_pre, mean controls, testable, interval width)."""
    out = defaultdict(list)
    joint = {THROUGH_MARCH: [], THROUGH_APRIL: []}
    for seed in SEEDS:
        sim = panel.simulate(cfg, seed)
        for through, teams in ((THROUGH_MARCH, MEASURED_MARCH), (THROUGH_APRIL, MEASURED_APRIL)):
            results = estimator.estimate(estimator_panel(sim, through), through)
            clean = True
            for team in teams:
                r = results[team]
                a = r.analysis
                truth = planted_effect(sim, team, through)
                covered = bool(a is not None and a.ci_lo <= truth <= a.ci_hi)
                out[(through, team)].append((
                    r.status, r.reason, covered, r.n_pre, float(np.mean([c.n_controls for c in a.cells])),
                    a.pretrend.testable, a.ci_hi - a.ci_lo,
                ))
                clean = clean and r.measured and covered
            joint[through].append(clean)
    return out, joint


def summarise(out):
    rows = {}
    for key, draws in out.items():
        measured = [d for d in draws if d[0] == "measured"]
        flagged = sum(d[1] == "pre_trend" for d in draws)
        rows[key] = {
            "n": len(draws),
            "measured": len(measured),
            "coverage": sum(d[2] for d in measured) / len(measured) if measured else math.nan,
            "coverage_any": sum(d[2] for d in draws) / len(draws),  # ignoring the pre-trend flag
            "width": float(np.mean([d[6] for d in measured])) if measured else math.nan,
            "flag_rate": flagged / len(draws),
            "untestable": sum(not d[5] for d in draws) / len(draws),
            "n_pre": draws[0][3],
            "controls": (min(d[4] for d in draws), max(d[4] for d in draws)),
        }
    return rows


def render(title, rows, joint):
    lines = [f"coverage simulation, {title}, {len(SEEDS)} seeds"]
    lines.append(f"  {'through':<8}{'team':<22}{'pre':>4}{'controls':>11}{'coverage':>10}{'any-flag':>10}{'width':>8}{'flagged':>9}{'untestable':>12}")
    for (through, team), r in rows.items():
        mark = " *" if team in CHANGING_CONTROL_SETS else ""
        lines.append(
            f"  {through:<8}{team:<22}{r['n_pre']:>4}{r['controls'][0]:>6.1f}-{r['controls'][1]:<4.1f}"
            f"{r['coverage']:>10.3f}{r['coverage_any']:>10.3f}{r['width']:>8.3f}{r['flag_rate']:>9.3f}{r['untestable']:>12.3f}{mark}"
        )
    for through, passed in joint.items():
        lines.append(f"  joint pass through {through} (all measured, all covered): {sum(passed) / len(passed):.3f}")
    lines.append("  * control sets change between cells")
    return "\n".join(lines)


@pytest.fixture(scope="module")
def emit(request):
    reporter = request.config.pluginmanager.getplugin("terminalreporter")

    def write(text):
        if reporter is not None:
            reporter.write_line("")
            for line in text.splitlines():
                reporter.write_line(line)
        else:
            print(text)

    return write


def gated(cfg, emit, title):
    out, joint = run_arm(cfg)
    rows = summarise(out)
    emit(render(title, rows, joint))
    for (through, team), r in rows.items():
        assert r["measured"] > 0, (title, through, team)
        assert r["coverage"] >= MIN_COVERAGE, (title, through, team, r["coverage"])
    return rows


def test_design_as_is(cfg, emit):
    rows = gated(cfg, emit, "design as is")
    emit(
        "pre-trend test, false-flag rate under the null (nominal 0.10; finding, not gated): "
        + ", ".join(f"{team} {r['flag_rate']:.3f}" for (through, team), r in rows.items() if through == THROUGH_MARCH)
    )
    assert {t for _, t in rows} == set(MEASURED_APRIL)
    assert rows[(THROUGH_MARCH, "support")]["n_pre"] == 3  # the one-degree-of-freedom team


def test_common_shock_set_to_zero(cfg, emit):
    gated(dataclasses.replace(cfg, month_shock_sd=0.0), emit, "common shock zero")


def test_common_shock_doubled(cfg, emit):
    gated(dataclasses.replace(cfg, month_shock_sd=2 * cfg.month_shock_sd), emit, "common shock doubled")


def test_control_noise_doubled_is_reported_not_gated(cfg, emit):
    teams = tuple(
        dataclasses.replace(t, noise_sd=2 * t.noise_sd) if t.team_id == CONTROL else t for t in cfg.teams
    )
    out, joint = run_arm(dataclasses.replace(cfg, teams=teams))
    rows = summarise(out)
    emit(render("control team noise sd doubled (reported, not gated)", rows, joint))
    assert all(0.0 <= r["coverage"] <= 1.0 for r in rows.values())


def test_pretrend_power_on_the_fixture_design(emit):
    """Reported: how often the pre-trend test flags a planted drift like `panel_pretrend.csv`'s."""
    months = [f"2025-{m:02d}" for m in range(1, 11)]
    shock = [0.00, 0.03, -0.02, 0.05, 0.01, -0.03, 0.04, 0.00, 0.02, -0.01]
    flagged = untestable = 0
    for seed in SEEDS:
        rng = np.random.default_rng(seed)
        y = {}
        for team, base in (("team_x", 3.0), ("team_p", 2.0), ("team_n1", 1.5), ("team_n2", 2.5)):
            y[team] = {m: base + shock[k] + rng.normal(0, 0.03) for k, m in enumerate(months)}
        for k in range(5):  # drift of 0.05 a month before the base month of team_x (g = m6)
            y["team_x"][months[k]] -= 0.05 * (4 - k)
        rollout = {"team_x": months[5], "team_p": months[6], "team_n1": None, "team_n2": None}
        r = estimator.estimate_team(estimator.Panel(y, rollout), "team_x", months[-1])
        flagged += r.reason == "pre_trend"
        untestable += not r.analysis.pretrend.testable
    emit(
        f"pre-trend test on a planted drift of 0.05 a month (4 leads), {len(SEEDS)} seeds: "
        f"flagged {flagged / len(SEEDS):.3f}, not testable {untestable / len(SEEDS):.3f}"
    )
    assert 0 <= flagged <= len(SEEDS)  # a finding, not a gate
