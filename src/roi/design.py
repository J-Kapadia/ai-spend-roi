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

Nothing here reads files or draws random numbers. The module must not be imported by
`roi.estimator` or by any pipeline module: it is for tests and for the report script that
sets the noise of each team.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy import stats

from roi.gen.config import GenConfig, TeamSpec, months_between
from roi.gen.panel import simulate

# The estimator's constants: a team needs this many months before and from its rollout.
MIN_PRE = 3
MIN_POST = 3


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
