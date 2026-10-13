"""Testing agenda: one designed experiment per unmeasured team.

Sample size per arm of a two-arm test with `L` months of repeated measures:

    n(L) = 2 (z_{1-alpha/2} + z_{1-beta})^2 sigma^2 [1 + (L-1) rho] / (L delta^2)

with alpha = 0.05 and power 0.80. `delta` is the break-even effect of a team with no ROI: its
current spend per head (the mean of its last three months) over the value per unit, in the
metric's per-head units; for a `team_level` metric its spend per month over the value per unit.
`sigma` is the user-month standard deviation: the standard deviation of ln(outcome) over the
team's months, times the baseline, times sqrt(team size); for a `team_level` metric the raw
standard deviation of the monthly value. Those months are the pre-rollout ones where the team
has at least two and otherwise all of them. A configured `sigma_override` replaces it. The test
length is the smallest L in 1..6 with ceil(n(L)) <= team_size / 2; if there is none the team is
`not_testable_alone` and should be paired with another team on a staggered start.

Each reason maps to exactly one design:

    no_pre_period, pre_trend, variance_not_computable   holdback      random seat holdback
    too_recent                                           wait          the smallest m in 1..12 with
                                                                       t sd sqrt((n+m+1) / (2 (n+m))) <= delta_log
    no_comparison                                        pair_stagger  the formula on the pair's combined headcount,
                                                                       the second team starting `min_pre` months later

For `wait`, n is the team's post-rollout months now, t the Student-t multiplier of its interval,
sd the root of the mean of its cell variances, and delta_log the break-even effect in log
points. The standard error never falls below sd / sqrt(2) (the base month is shared by every
cell), so when t sd / sqrt(2) is above delta_log waiting cannot help and the item says so and
offers the holdback instead.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from scipy import stats

from roi import alloc, curve, estimator
from roi.workdir import RoiError

ALPHA = 0.05
POWER = 0.80
MAX_LENGTH = 6
MAX_WAIT = 12
HOLDBACK, WAIT, PAIR_STAGGER, NOT_TESTABLE = "holdback", "wait", "pair_stagger", "not_testable_alone"
DESIGN_BY_REASON = {
    estimator.NO_PRE_PERIOD: HOLDBACK,
    estimator.PRE_TREND: HOLDBACK,
    estimator.VARIANCE_NOT_COMPUTABLE: HOLDBACK,
    estimator.TOO_RECENT: WAIT,
    estimator.NO_COMPARISON: PAIR_STAGGER,
}
Z = float(stats.norm.ppf(1.0 - ALPHA / 2.0) + stats.norm.ppf(POWER))


@dataclass(frozen=True)
class Inputs:
    team_id: str
    reason: str
    kind: str
    direction: int
    team_size: int
    baseline: float
    baseline_basis: str  # pre | all
    pre_levels: tuple[float, ...]  # outcome levels before rollout
    post_levels: tuple[float, ...]
    x0_cents: float  # current spend per head, cents per month
    value_per_unit: float  # base, dollars
    rho: float = alloc.DEFAULT_RHO
    sigma_override: float | None = None
    pair_size: int = 0  # headcount of the partner team (pair_stagger)
    n_post: int = 0  # wait: post-rollout months now
    df: int = 0
    cell_variances: tuple[float, ...] = ()  # wait: variances of the team's cells, log points squared


@dataclass(frozen=True)
class Item:
    team_id: str
    design: str
    n_per_arm: int | None
    length_months: int | None
    delta: float | None
    sigma: float | None
    rho: float
    alpha: float
    power: float
    note: str
    baseline_basis: str


def sample_size(sigma: float, delta: float, rho: float, length: int) -> float:
    return 2.0 * Z**2 * sigma**2 * (1.0 + (length - 1) * rho) / (length * delta**2)


def holdback(sigma: float, delta: float, rho: float, size: float) -> tuple[int, int] | None:
    """(n per arm, length in months) of the shortest test that fits half the team, or None."""
    for length in range(1, MAX_LENGTH + 1):
        n = math.ceil(sample_size(sigma, delta, rho, length) - 1e-9)
        if n <= size / 2.0:
            return n, length
    return None


def sigma_of(i: Inputs) -> float | None:
    """The user-month standard deviation, or None when the team has fewer than two months."""
    if i.sigma_override is not None:
        return i.sigma_override
    levels = i.pre_levels if len(i.pre_levels) >= 2 else i.pre_levels + i.post_levels
    if len(levels) < 2:
        return None
    if i.kind == alloc.TEAM_LEVEL:
        return float(np.std(levels, ddof=1))
    return float(np.std(np.log(levels), ddof=1)) * i.baseline * math.sqrt(i.team_size)


def breakeven(i: Inputs) -> float:
    dollars = i.x0_cents / 100.0
    if i.kind == alloc.TEAM_LEVEL:
        dollars *= i.team_size
    return dollars / i.value_per_unit


def delta_log(delta: float, baseline: float, direction: int) -> float | None:
    """The break-even effect in log points, or None when it is out of range."""
    ratio = delta / baseline
    if direction >= 0:
        return math.log1p(ratio)
    return -math.log1p(-ratio) if ratio < 1.0 else None


def _item(i: Inputs, design: str, n: int | None, length: int | None, delta: float | None, sigma: float | None,
          note: str) -> Item:
    return Item(i.team_id, design, n, length, delta, sigma, i.rho, ALPHA, POWER, note, i.baseline_basis)


def _fixed(i: Inputs, design: str, size: float, delta: float, sigma: float, what: str) -> Item:
    got = holdback(sigma, delta, i.rho, size)
    if got is None:
        n6 = math.ceil(sample_size(sigma, delta, i.rho, MAX_LENGTH) - 1e-9)
        return _item(i, NOT_TESTABLE, None, None, delta, sigma,
                     f"needs {n6} per arm over {MAX_LENGTH} months, more than half of {size:g}; "
                     "pair with another team on a staggered start")
    return _item(i, design, got[0], got[1], delta, sigma, what)


def design(i: Inputs) -> Item:
    if i.reason not in DESIGN_BY_REASON:
        raise RoiError(2, f"team {i.team_id}: no design for reason {i.reason!r}")
    delta = breakeven(i)
    if i.reason == estimator.TOO_RECENT:
        return _wait(i, delta)
    sigma = sigma_of(i)
    if sigma is None:
        return _item(i, NOT_TESTABLE, None, None, delta, None, "fewer than two months of outcome")
    if i.reason == estimator.NO_COMPARISON:
        size = i.team_size + i.pair_size
        return _fixed(i, PAIR_STAGGER, size, delta, sigma,
                      f"combined headcount {size}; the second team starts {estimator.MIN_PRE} months after the first")
    return _fixed(i, HOLDBACK, i.team_size, delta, sigma, "random seat holdback")


def _wait(i: Inputs, delta: float) -> Item:
    target = delta_log(delta, i.baseline, i.direction)
    if target is None or not i.cell_variances or i.df < 1:
        return _item(i, NOT_TESTABLE, None, None, delta, None, "no usable post-rollout month yet")
    sd = math.sqrt(sum(i.cell_variances) / len(i.cell_variances))
    t = float(stats.t.ppf(0.5 + estimator.LEVEL / 2.0, i.df))
    for m in range(1, MAX_WAIT + 1):
        n = i.n_post + m
        if t * sd * math.sqrt((n + 1) / (2.0 * n)) <= target:
            return _item(i, WAIT, None, m, target, sd,
                         f"wait {m} more months of post-rollout data; delta and sigma in log points")
    if t * sd / math.sqrt(2.0) > target:
        sigma = sigma_of(i)
        offer = "holdback not possible: fewer than two months of outcome"
        if sigma is not None:
            got = holdback(sigma, delta, i.rho, i.team_size)
            offer = (f"holdback instead: {got[0]} per arm over {got[1]} months" if got
                     else "holdback instead is not testable alone either")
        return _item(i, NOT_TESTABLE, None, None, target, sd,
                     f"the base-month noise does not shrink with waiting; {offer}; delta and sigma in log points")
    return _item(i, NOT_TESTABLE, None, None, target, sd,
                 f"waiting {MAX_WAIT} months does not reach the break-even effect; delta and sigma in log points")


# ---- inputs from the warehouse ---------------------------------------------------------------

def items_from_warehouse(con, through: str, cfg: alloc.Config) -> list[Item]:
    """One item for each unmeasured team at `through`, in team-id order. Raises RoiError(2) naming
    an unmeasured team that has no value per unit."""
    teams = con.execute(
        "SELECT e.team_id, e.reason, d.kind, d.direction, e.baseline_outcome, e.baseline_basis, e.n_post"
        " FROM est_team_effect e JOIN dim_team d ON d.team_id = e.team_id"
        " WHERE e.through_month = ? AND e.status = 'unmeasured' ORDER BY e.team_id",
        [through],
    ).fetchall()
    if not teams:
        return []
    profile = curve.spend_profile(con, through)
    panel = estimator.read_panel(con, through)
    size = {t: profile[t][2] for t in profile}
    unmeasured = sorted(t[0] for t in teams)

    items = []
    for team, reason, kind, direction, baseline, basis, n_post in teams:
        g = panel.rollout.get(team)
        levels = sorted(panel.outcome.get(team, {}).items())
        pre = tuple(v for m, v in levels if g is None or m < g)
        post = tuple(v for m, v in levels if g is not None and m >= g)
        analysis = estimator.analyse(panel, team, through) if reason == estimator.TOO_RECENT else None
        partners = sorted((size[t], t) for t in unmeasured if t != team and t in size)
        items.append(design(Inputs(
            team, reason, kind, int(direction), size[team], float(baseline), basis, pre, post,
            profile[team][0], cfg.values(team)[1], cfg.rho, cfg.sigma_override.get(team),
            partners[0][0] if partners else 0, n_post,
            analysis.df if analysis else 0,
            tuple(c.var for c in analysis.cells) if analysis else (),
        )))
    return items


# ---- the fixture check -------------------------------------------------------------------------

def fixture_inputs(row: dict) -> Inputs:
    fields = dict(row["inputs"])
    for key in ("pre_levels", "post_levels", "cell_variances"):
        fields[key] = tuple(fields.get(key, ()))
    return Inputs(**fields)


def check_fixture(fixture: dict) -> list[tuple[str, bool, str]]:
    """Run every fixture row through `design` and compare with its hand-worked expectation.

    Returns (id, pass, detail) triples: one per row, plus a last one that every reason in the
    design table appears in the fixture and maps to one design."""
    checks = []
    for row in fixture["rows"]:
        got = design(fixture_inputs(row))
        want = row["expected"]
        bad = []
        for field in ("design", "n_per_arm", "length_months"):
            if getattr(got, field) != want[field]:
                bad.append(f"{field} {getattr(got, field)} != {want[field]}")
        for field in ("sigma", "delta"):
            a, b = getattr(got, field), want[field]
            if (a is None) != (b is None) or (a is not None and not math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-12)):
                bad.append(f"{field} {a} != {b}")
        checks.append((row["id"], not bad, "; ".join(bad) or f"{got.design}: n={got.n_per_arm}, length={got.length_months}"))
    covered = {row["inputs"]["reason"] for row in fixture["rows"]}
    missing = sorted(set(estimator.REASONS) - covered)
    checks.append(("reasons", not missing and all(r in DESIGN_BY_REASON for r in estimator.REASONS),
                   f"no fixture row for {', '.join(missing)}" if missing else "every reason has a fixture row and one design"))
    return checks


def load_fixture(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except OSError:
        raise RoiError(2, f"cannot read the fixture {path}") from None
