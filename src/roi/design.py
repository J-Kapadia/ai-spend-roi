"""The analytic standard error of a team effect under the generator's data-generating process.

This is the pure counterpart of `roi.estimator` for data that `roi.gen.panel` simulates: the
outcome of team i in month s is a planted effect, one month shock common to every team, and
team-month noise with sd sigma_i. The estimator forms, for a post month t, the backcast series
delta_s = Y_i,s - mean of the controls valid in t, and takes ATT(g, t) = delta_t - delta_{g-1}.
The common shock is in both terms of delta and cancels, and the months are independent, so
with C_t the controls of cell t:

    Var[cell t]     = 2 (sigma_i^2 + sum_{c in C_t} sigma_c^2 / |C_t|^2)
    Cov[t, t']      = sigma_i^2 + sum_{c in C_t & C_t'} sigma_c^2 / (|C_t| |C_t'|)

(the cells share only the base month g-1). The team effect is the equal-weight mean of the
cells, with variance w' Sigma w for w = 1 / n_post, degrees of freedom n_pre - 2 and a Student-t
97.5% multiplier.

`solve_noise` sets the noise of each team so that the planted effect is three standard errors
from zero, and `marginal_se` says whether the ramp identifies the dose-response curve.

Nothing here reads files or draws random numbers. The module must not be imported by
`roi.estimator` or by any pipeline module: it is for tests and for the report script that
sets the noise of each team.
"""

from __future__ import annotations

import dataclasses
import math
import statistics
from dataclasses import dataclass

import numpy as np
from scipy import stats

from roi.curve import CURRENT_MONTHS, usable_covariance
from roi.gen.config import GenConfig, TeamSpec, months_between
from roi.gen.panel import simulate

# The estimator's constants: a team needs this many months before and from its rollout.
MIN_PRE = 3
MIN_POST = 3

# The teams whose noise is solved, and how: the first five are measured through the last month
# written to the books, legal_operations through the held-back month. The remaining teams (the
# control and the one not measured) carry the median of the first five.
MEASURED_AT_LAST = ("support", "sales_development", "product_engineering", "marketing", "finance_operations")
MEASURED_AT_INCOMING = ("legal_operations",)
# The sd given to a team whose controls' noise alone already exceeds what the target allows.
NOISE_FLOOR = 1e-6


@dataclass(frozen=True)
class TeamDesign:
    team_id: str
    through: str
    n_pre: int
    n_post: int
    cells: tuple[str, ...]  # post months g..through with at least one control
    controls_per_cell: tuple[int, ...]
    sigma: np.ndarray  # covariance of the cells' ATTs, in log points squared
    se: float  # sqrt(w' Sigma w) with equal weights over the cells
    df: int
    t_mult: float
    planted: float  # mean planted effect over the cells, log points
    ratio: float  # planted / se


def _spec(cfg: GenConfig, team: str) -> TeamSpec:
    for spec in cfg.teams:
        if spec.team_id == team:
            return spec
    raise ValueError(f"unknown team {team!r}")


def _months(cfg: GenConfig, through: str) -> list[str]:
    months = months_between(cfg.first_month, through)
    if not months or through > cfg.incoming_month:
        raise ValueError(f"through {through} is outside the panel {cfg.first_month}..{cfg.incoming_month}")
    return months


def _window(cfg: GenConfig, team: str, through: str) -> tuple[str, int, int]:
    """The rollout month and the counts of pre and post months, or ValueError when the team
    has no rollout (it is a control) or too few months on either side to be measured."""
    g = _spec(cfg, team).rollout_month
    if g is None:
        raise ValueError(f"{team} is the control team: it has no effect to measure")
    months = _months(cfg, through)
    n_pre = sum(m < g for m in months)
    n_post = sum(m >= g for m in months)
    if n_pre < MIN_PRE:
        raise ValueError(f"{team} has {n_pre} months before its rollout {g}; at least {MIN_PRE} are needed")
    if n_post < MIN_POST:
        raise ValueError(f"{team} has {n_post} months from its rollout {g} through {through}; at least {MIN_POST} are needed")
    return g, n_pre, n_post


def _previous(month: str) -> str:
    y, m = int(month[:4]), int(month[5:])
    return f"{y - 1:04d}-12" if m == 1 else f"{y:04d}-{m - 1:02d}"


def control_set(cfg: GenConfig, team: str, t: str) -> tuple[str, ...]:
    """The never-treated teams and the teams whose rollout is after max(t, g-1), sorted."""
    g = _spec(cfg, team).rollout_month
    if g is None:
        raise ValueError(f"{team} is the control team: it has no effect to measure")
    horizon = max(t, _previous(g))
    return tuple(sorted(
        s.team_id for s in cfg.teams
        if s.team_id != team and (s.rollout_month is None or s.rollout_month > horizon)
    ))


def cells(cfg: GenConfig, team: str, through: str) -> tuple[str, ...]:
    """The post months g..through that have at least one control."""
    g, _, _ = _window(cfg, team, through)
    return tuple(t for t in months_between(g, through) if control_set(cfg, team, t))


def sigma(cfg: GenConfig, team: str, through: str) -> np.ndarray:
    """Covariance matrix of the team's cell ATTs, one row and column per month of `cells`."""
    noise = {s.team_id: s.noise_sd**2 for s in cfg.teams}
    own = noise[team]
    sets = [control_set(cfg, team, t) for t in cells(cfg, team, through)]
    n = len(sets)
    out = np.empty((n, n))
    for a in range(n):
        for b in range(n):
            shared = sum(noise[c] for c in set(sets[a]) & set(sets[b])) / (len(sets[a]) * len(sets[b]))
            out[a, b] = 2.0 * (own + shared) if a == b else own + shared
    return out


def team_effect_se(cfg: GenConfig, team: str, through: str) -> float:
    cov = sigma(cfg, team, through)
    w = np.full(len(cov), 1.0 / len(cov))
    return float(np.sqrt(w @ cov @ w))


def df(cfg: GenConfig, team: str, through: str) -> int:
    """Degrees of freedom of the variances: the pre-period changes, less one."""
    return _window(cfg, team, through)[1] - 2


def t_multiplier(degrees: int) -> float:
    return float(stats.t.ppf(0.975, degrees))


def planted_team_effect(cfg: GenConfig, team: str, through: str) -> float:
    """Mean planted effect over the cells; the effect path does not depend on the draw."""
    months = _months(cfg, through)
    effect = simulate(cfg).team(team).effect
    return float(np.mean([effect[months.index(t)] for t in cells(cfg, team, through)]))


def team_design(cfg: GenConfig, team: str, through: str) -> TeamDesign:
    _, n_pre, _ = _window(cfg, team, through)
    cell_months = cells(cfg, team, through)
    if len(cell_months) < MIN_POST:
        raise ValueError(f"{team} has {len(cell_months)} post months with a control through {through}; at least {MIN_POST} are needed")
    se = team_effect_se(cfg, team, through)
    planted = planted_team_effect(cfg, team, through)
    return TeamDesign(
        team_id=team,
        through=through,
        n_pre=n_pre,
        n_post=len(cell_months),
        cells=cell_months,
        controls_per_cell=tuple(len(control_set(cfg, team, t)) for t in cell_months),
        sigma=sigma(cfg, team, through),
        se=se,
        df=n_pre - 2,
        t_mult=t_multiplier(n_pre - 2),
        planted=planted,
        ratio=planted / se,
    )


def report(cfg: GenConfig, through: str) -> dict[str, TeamDesign]:
    """The design of every team measurable at `through`, in team id order."""
    out = {}
    for spec in sorted(cfg.teams, key=lambda s: s.team_id):
        try:
            out[spec.team_id] = team_design(cfg, spec.team_id, through)
        except ValueError:
            continue
    return out


def _with_noise(cfg: GenConfig, sds: dict[str, float]) -> GenConfig:
    teams = tuple(dataclasses.replace(s, noise_sd=sds[s.team_id]) for s in cfg.teams)
    return dataclasses.replace(cfg, teams=teams)


def solve_noise(cfg: GenConfig, target: float = 3.0, tol: float = 1e-6, max_iter: int = 200) -> dict[str, float]:
    """The noise sd of every team such that planted effect / standard error is `target`.

    The five teams measured through the last month and legal_operations through the held-back
    month are solved jointly: the variance of a team's effect is linear in its own noise
    variance, with the other teams' noise entering through its controls, so each pass solves
    every team's own sd given the others' sds from the previous pass, until the largest
    relative change is below `tol`. The control and the unmeasured team then take the median
    of the five. A team whose controls' noise alone already exceeds what the target allows (a
    small planted effect beside noisy controls) gets `NOISE_FLOOR`, and its ratio is then below
    the target. Raises RuntimeError when the iteration does not converge.
    """
    targets = [(t, cfg.last_month) for t in MEASURED_AT_LAST] + [(t, cfg.incoming_month) for t in MEASURED_AT_INCOMING]
    solved = [t for t, _ in targets]
    planted = {t: planted_team_effect(cfg, t, through) for t, through in targets}
    sds = {s.team_id: s.noise_sd for s in cfg.teams}
    for _ in range(max_iter):
        median = statistics.median(sds[t] for t in MEASURED_AT_LAST)
        prev = {**sds, **{s.team_id: median for s in cfg.teams if s.team_id not in solved}}
        nxt = dict(prev)
        for team, through in targets:
            zero = team_effect_se(_with_noise(cfg, {**prev, team: 0.0}), team, through) ** 2
            one = team_effect_se(_with_noise(cfg, {**prev, team: 1.0}), team, through) ** 2
            room = (planted[team] / target) ** 2 - zero
            nxt[team] = max(NOISE_FLOOR, math.sqrt(max(room, 0.0) / (one - zero)))
        change = max(abs(nxt[t] - prev[t]) / prev[t] for t in solved)
        sds = nxt
        if change < tol:
            median = statistics.median(sds[t] for t in MEASURED_AT_LAST)
            return {**sds, **{s.team_id: median for s in cfg.teams if s.team_id not in solved}}
    raise RuntimeError(f"solve_noise did not converge in {max_iter} passes (last relative change {change:.3g})")


def _curve_inputs(cfg: GenConfig, team: str, through: str) -> tuple[np.ndarray, float, float, np.ndarray]:
    """Spend per head of the cells (cents), the planted A and k (k in cents) and Sigma."""
    cell_months = cells(cfg, team, through)
    if len(cell_months) < MIN_POST:
        raise ValueError(f"{team} has {len(cell_months)} post months with a control through {through}; at least {MIN_POST} are needed")
    tp = simulate(cfg).team(team)
    months = _months(cfg, through)
    x = np.array([tp.spend_cents[months.index(t)] / tp.spec.headcount for t in cell_months], dtype=float)
    return x, tp.curve_a, tp.curve_k * 100.0, sigma(cfg, team, through)


def marginal_se(cfg: GenConfig, team: str, through: str) -> float:
    """Standard error of the marginal return at the current spend, by the delta method.

    The covariance of (A, k) is (J' Sigma^-1 J)^-1 at the planted values, for J the Jacobian of
    the effect in (A, k) over the cells' spend per head; the marginal return A / k exp(-x0 / k)
    is taken at x0 = the mean of the last three months' spend per head.
    """
    x, a, k, cov = _curve_inputs(cfg, team, through)
    e = np.exp(-x / k)
    jac = np.column_stack([1.0 - e, -a * x / k**2 * e])
    info = jac.T @ np.linalg.solve(usable_covariance(cov), jac)
    theta_cov = np.linalg.inv(info)
    x0 = float(np.mean(x[-CURRENT_MONTHS:]))
    e0 = math.exp(-x0 / k)
    grad = np.array([e0 / k, a * e0 * (x0 - k) / k**3])
    return float(math.sqrt(grad @ theta_cov @ grad))


def identified_at_design(cfg: GenConfig, team: str, through: str) -> bool:
    """Whether the planted marginal return is above zero by more than its one-sided 95% bound."""
    x, a, k, _ = _curve_inputs(cfg, team, through)
    x0 = float(np.mean(x[-CURRENT_MONTHS:]))
    marg = a / k * math.exp(-x0 / k)
    t = float(stats.t.ppf(0.95, len(x) - 2))
    return marg - t * marginal_se(cfg, team, through) > 0.0
