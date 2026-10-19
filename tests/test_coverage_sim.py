"""500-seed coverage simulation of the estimator's 95% interval, on `roi.gen.panel`.

Each seed simulates the team-month panel of the generator and estimates every team. For each
measured team the share of seeds in which the interval holds a planted team effect must be at
least 0.90; there is no upper limit (the interval over-covers for teams with one or two pre-period
changes, which only makes it more cautious) and each team's mean interval width is printed. Arms:
the design as is, the common month shock set to zero and to twice its value (all three gated), and
the control team's noise sd doubled (reported, not gated). The pre-trend test's false-flag rate
under the null (the generator plants no drift) and its detection rate on a planted drift are
findings, printed and not gated.

Runs on the shipped design, `config/gen.yaml`. Two further arms are reported, not gated: a
learning effect (the effect grows with months since rollout to the same plateau as the spend
curve, which the fit cannot tell from diminishing returns) and, from the printed joint pass rate,
the number of re-registrations after which a correct method has failed too often to be believed.
The wall time of every arm is recorded and the sum is held to a budget.
"""

import dataclasses
import math
import time
from collections import defaultdict
from pathlib import Path

import numpy as np
import pytest

from roi import curve, estimator
from roi.gen import config, panel

pytestmark = pytest.mark.slow

CONFIG = Path(__file__).resolve().parents[1] / "config" / "gen.yaml"
SEEDS = range(1, 501)
LEARNING_SEEDS = range(1, 101)
BUDGET_SECONDS = 240.0
RETRY_RISK = 0.10  # a correct method may fail r + 1 independent draws with at most this probability
TIMES: dict[str, float] = {}
MIN_COVERAGE = 0.90
THROUGH_MARCH, THROUGH_APRIL = "2026-03", "2026-04"
MEASURED_MARCH = ("support", "sales_development", "product_engineering", "marketing", "finance_operations")
MEASURED_APRIL = (*MEASURED_MARCH, "legal_operations")
CONTROL = "people_operations"
# The teams whose control sets change from month to month, named in the printed table.
CHANGING_CONTROL_SETS = ("sales_development", "legal_operations")


@pytest.fixture(scope="module")
def cfg():
    return config.load(CONFIG)


@pytest.fixture(autouse=True)
def wall_time(request):
    start = time.perf_counter()
    yield
    TIMES[request.node.name] = time.perf_counter() - start


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


def reregistration_limit(p):
    """The largest r such that failing r + 1 independent draws has probability at most RETRY_RISK
    when each draw passes with probability p; None when one failed draw is already that rare."""
    if p >= 1.0 - RETRY_RISK:
        return None
    if p <= 0.0:
        return math.inf
    return math.ceil(math.log(RETRY_RISK) / math.log(1.0 - p)) - 1


def render_registration(rows, joint):
    lines = []
    for through, passed in joint.items():
        p = sum(passed) / len(passed)
        r = reregistration_limit(p)
        limit = "no limit needed" if r is None else "none (no draw passes)" if math.isinf(r) else str(r)
        lines.append(f"  re-registration limit through {through}: p = {p:.3f}, r = {limit}")
    lines.append("  pre-trend test size under the null (nominal 0.10) by pre-month count:")
    for (through, team), r in rows.items():
        lines.append(f"    {through:<8}{team:<22}pre months {r['n_pre']:>2}  false-flag rate {r['flag_rate']:.3f}")
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
    return rows, joint


def test_design_as_is(cfg, emit):
    rows, joint = gated(cfg, emit, "design as is")
    emit(render_registration(rows, joint))
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
    assert 0 <= flagged <= len(SEEDS)  # a finding, not a failure


def learning_panel(cfg, sim):
    """`sim` with each rolled-out team's planted effect replaced by a learning path,
    effect_at_target x (1 - exp(-m / ramp_months)) with m the months since rollout (0 in the first
    month), in log points of the direction-normalised outcome: ln(1 + effect_at_target) is the
    plateau of the spend curve at the target spend. The outcome is rebuilt from the simulated
    noise: the level moves by the difference of the two effect paths."""
    teams = []
    for tp in sim.teams:
        g = tp.spec.rollout_month
        if g is None:
            teams.append(tp)
            continue
        start = sim.months.index(g)
        plateau = math.log1p(tp.spec.effect_at_target)
        effect = np.array([
            plateau * (1.0 - math.exp(-(k - start) / cfg.ramp_months)) if k >= start else 0.0
            for k in range(len(sim.months))
        ])
        outcome = np.exp(tp.spec.direction * (tp.spec.direction * np.log(tp.outcome) - tp.effect + effect))
        output = outcome * tp.spec.headcount if tp.spec.kind == "additive" else outcome
        teams.append(dataclasses.replace(tp, effect=effect, outcome=outcome, output=output))
    return dataclasses.replace(sim, teams=tuple(teams))


def fit_curves(sim, through):
    """Per measured team: the fitted curve from the estimator's cells, or None when not measured."""
    results = estimator.estimate(estimator_panel(sim, through), through)
    fits = {}
    for team in MEASURED_MARCH:
        r = results[team]
        if not r.measured:
            fits[team] = None
            continue
        tp = sim.team(team)
        per_head = {m: float(tp.spend_cents[k]) / tp.spec.headcount for k, m in enumerate(sim.months) if m <= through}
        x = np.array([per_head[c.t] for c in r.analysis.cells])
        y = np.array([c.att for c in r.analysis.cells])
        x0 = float(np.mean(list(per_head.values())[-curve.CURRENT_MONTHS:]))
        fits[team] = curve.fit(x, y, r.analysis.cov, x0)
    return fits


def test_learning_effect_is_reported_not_gated(cfg, emit):
    """Reported: the curve is fitted to a spend-driven and to a learning-driven effect with the same
    plateau. The fit sees spend per head against the effect and has no way to tell the two apart."""
    arms = {"spend-driven": defaultdict(list), "learning-driven": defaultdict(list)}
    for seed in LEARNING_SEEDS:
        sim = panel.simulate(cfg, seed)
        for name, s in (("spend-driven", sim), ("learning-driven", learning_panel(cfg, sim))):
            for team, f in fit_curves(s, THROUGH_MARCH).items():
                if f is not None and f.marginal_at_current is not None:
                    arms[name][team].append((f.marginal_at_current, f.identified))
    lines = [f"learning effect, same plateau as the spend curve, {len(LEARNING_SEEDS)} seeds (reported, not gated)"]
    lines.append(f"  {'team':<22}{'effect':<17}{'fitted':>8}{'mean marginal at current':>26}{'identified':>12}")
    for team in MEASURED_MARCH:
        for name, by_team in arms.items():
            draws = by_team[team]
            marginal = float(np.mean([d[0] for d in draws])) if draws else math.nan
            share = sum(d[1] for d in draws) / len(draws) if draws else math.nan
            lines.append(f"  {team:<22}{name:<17}{len(draws):>8}{marginal:>26.3e}{share:>12.3f}")
            assert math.isfinite(marginal) and math.isfinite(share), (team, name)
    lines.append("  marginal in log points per cent of monthly spend per head; fitted = seeds with a measured team")
    emit("\n".join(lines))


def test_all_arms_fit_the_time_budget(emit):
    """Last in the module: the arms above ran in file order and left their wall times in TIMES."""
    total = sum(TIMES.values())
    emit(
        "coverage simulation wall time by arm: "
        + ", ".join(f"{name} {seconds:.1f} s" for name, seconds in TIMES.items())
        + f"; total {total:.1f} s of {BUDGET_SECONDS:.0f} s"
    )
    assert total < BUDGET_SECONDS, (total, TIMES)
