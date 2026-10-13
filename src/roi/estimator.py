"""Callaway-Sant'Anna group-time effects on the log outcome, one cohort per team.

Pure functions over a `Panel`; the only I/O is `read_panel`, which reads the estimator input
mart from a DuckDB connection the caller opened. Nothing here reads the truth file.

For team i that first has AI spend in month g, with pre-period months S (the team's months
before g, base month b = the last of them), and a month t of the window:

    C_t   = control teams with no spend through max(t, b), observed in every month of S and in t
    d_s   = Y[i, s] - mean over c in C_t of Y[c, s]      the control-adjusted backcast series
    ATT(g, t) = d_t - d_b

The common month shock is in both terms of d, so it cancels and the variance is taken on d:

    Var ATT(g, t)  = sample variance (n - 1) of the one-period changes of d over S
    Cov(t, t')     = sample covariance (n - 1) of the level series d^(t), d^(t') over S

The team effect is the equal-weight mean of the valid post-rollout cells, its variance is
w' Sigma w, and its 95% interval uses Student's t on df = n_pre - 2 (every variance rests on
n_pre - 1 changes). The pre-trend test is a Wald test on the pre-period leads with an F(L, df)
reference at 10%.

When w' Sigma w is not positive the interval is not computable and the team is `unmeasured`
with reason `variance_not_computable`; no zero-width interval is ever reported.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field

import numpy as np
from scipy import stats

ESTIMATOR_VERSION = "cs-1"
MIN_PRE = 3
MIN_POST = 3
LEVEL = 0.95
PRETREND_ALPHA = 0.10

MEASURED, UNMEASURED, CONTROL = "measured", "unmeasured", "control"
NO_PRE_PERIOD, TOO_RECENT, NO_COMPARISON, PRE_TREND = "no_pre_period", "too_recent", "no_comparison", "pre_trend"
VARIANCE_NOT_COMPUTABLE = "variance_not_computable"
REASONS = (NO_PRE_PERIOD, TOO_RECENT, NO_COMPARISON, PRE_TREND, VARIANCE_NOT_COMPUTABLE)


@dataclass(frozen=True)
class Panel:
    """The estimator input: one value per team and month, months as sorted `YYYY-MM` strings.

    `y` is Y = direction x ln(outcome); a month without a value is absent. `rollout` is the
    first month with spend (None: never treated). `outcome` holds the levels for baselines.
    """

    y: Mapping[str, Mapping[str, float]]
    rollout: Mapping[str, str | None]
    outcome: Mapping[str, Mapping[str, float]] = field(default_factory=dict)


@dataclass(frozen=True)
class Cell:
    g: str
    t: str
    att: float
    var: float
    n_controls: int
    controls: tuple[str, ...]

    @property
    def se(self) -> float:
        return float(np.sqrt(self.var))


@dataclass(frozen=True)
class PreTrend:
    leads: tuple[Cell, ...]
    cov: np.ndarray
    testable: bool  # at least two leads and a positive-definite covariance
    statistic: float | None  # a' Sigma^-1 a / L
    p_value: float | None
    flagged: bool


@dataclass(frozen=True)
class Analysis:
    """Everything computed for a team that has a window, whatever its final status."""

    cells: tuple[Cell, ...]  # post-rollout cells with a valid control set, by month
    cov: np.ndarray  # Sigma of the cells: variances on the diagonal
    effect: float
    var: float  # w' Sigma w as computed; not positive when the interval is not computable
    se: float  # NaN when var is not positive
    df: int
    t_mult: float
    ci_lo: float  # NaN when var is not positive
    ci_hi: float
    pretrend: PreTrend

    @property
    def computable(self) -> bool:
        return self.var > 0.0


@dataclass(frozen=True)
class TeamResult:
    team_id: str
    status: str
    reason: str | None
    n_pre: int
    n_post: int
    baseline_outcome: float | None
    baseline_basis: str | None  # pre | all
    analysis: Analysis | None  # present for every team with a window; only measured teams report it

    @property
    def measured(self) -> bool:
        return self.status == MEASURED

    @property
    def effect(self) -> float | None:
        return self.analysis.effect if self.measured else None

    @property
    def ci_lo(self) -> float | None:
        return self.analysis.ci_lo if self.measured else None

    @property
    def ci_hi(self) -> float | None:
        return self.analysis.ci_hi if self.measured else None

    @property
    def df(self) -> int | None:
        return self.analysis.df if self.measured else None


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs)


def _sample_cov(x: list[float], y: list[float]) -> float:
    mx, my = _mean(x), _mean(y)
    return sum((a - mx) * (b - my) for a, b in zip(x, y)) / (len(x) - 1)


def _controls(panel: Panel, team: str, base: str, t: str, pre: list[str]) -> tuple[str, ...]:
    """Controls valid for a cell: untreated through max(t, base) and observed on pre and t."""
    horizon = max(t, base)
    out = []
    for c in sorted(panel.y):
        g_c = panel.rollout.get(c)
        if c == team or (g_c is not None and g_c <= horizon):
            continue
        if t in panel.y[c] and all(s in panel.y[c] for s in pre):
            out.append(c)
    return tuple(out)


def _backcast(panel: Panel, team: str, controls: tuple[str, ...], months: list[str]) -> dict[str, float]:
    return {s: panel.y[team][s] - _mean([panel.y[c][s] for c in controls]) for s in months}


def _cell(panel: Panel, team: str, g: str, t: str, pre: list[str]) -> tuple[Cell, list[float]] | None:
    """ATT(g, t) with its variance, and the backcast level series over the pre-period."""
    base = pre[-1]
    controls = _controls(panel, team, base, t, pre)
    if not controls:
        return None
    d = _backcast(panel, team, controls, pre + ([] if t in pre else [t]))
    levels = [d[s] for s in pre]
    changes = [b - a for a, b in zip(levels, levels[1:])]
    cell = Cell(g, t, d[t] - d[base], _sample_cov(changes, changes), len(controls), controls)
    return cell, levels


def _cov_matrix(cells: list[Cell], series: list[list[float]]) -> np.ndarray:
    n = len(cells)
    cov = np.empty((n, n))
    for a in range(n):
        cov[a, a] = cells[a].var
        for b in range(a + 1, n):
            cov[a, b] = cov[b, a] = _sample_cov(series[a], series[b])
    return cov


def _pretrend(panel: Panel, team: str, g: str, pre: list[str], df: int, alpha: float) -> PreTrend:
    leads, series = [], []
    for t in pre[:-1]:
        got = _cell(panel, team, g, t, pre)
        if got is not None:
            leads.append(got[0])
            series.append(got[1])
    if len(leads) < 2:
        return PreTrend(tuple(leads), np.empty((0, 0)), False, None, None, False)
    cov = _cov_matrix(leads, series)
    eig = np.linalg.eigvalsh(cov)
    if eig.min() <= 1e-12 * max(abs(eig.max()), 1e-300):
        return PreTrend(tuple(leads), cov, False, None, None, False)
    a = np.array([c.att for c in leads])
    stat = float(a @ np.linalg.solve(cov, a)) / len(leads)
    p = float(stats.f.sf(stat, len(leads), df))
    return PreTrend(tuple(leads), cov, True, stat, p, p < alpha)


def analyse(panel: Panel, team: str, through: str, *, alpha: float = PRETREND_ALPHA) -> Analysis | None:
    """The cells, Sigma, effect, interval and pre-trend test for one treated team, or None
    when the team has no pre-period, no post-period or no valid cell."""
    g = panel.rollout.get(team)
    months = sorted(m for m in panel.y[team] if m <= through)
    if g is None:
        return None
    pre = [m for m in months if m < g]
    if len(pre) < 3:
        return None
    cells, series = [], []
    for t in (m for m in months if m >= g):
        got = _cell(panel, team, g, t, pre)
        if got is not None:
            cells.append(got[0])
            series.append(got[1])
    if not cells:
        return None
    df = len(pre) - 2
    cov = _cov_matrix(cells, series)
    w = np.full(len(cells), 1.0 / len(cells))
    effect = float(sum(c.att for c in cells) / len(cells))
    var = float(w @ cov @ w)
    se = float(np.sqrt(var)) if var > 0.0 else math.nan
    t_mult = float(stats.t.ppf(0.5 + LEVEL / 2, df))
    return Analysis(
        tuple(cells), cov, effect, var, se, df, t_mult, effect - t_mult * se, effect + t_mult * se,
        _pretrend(panel, team, g, pre, df, alpha),
    )


def _baseline(panel: Panel, team: str, through: str) -> tuple[float | None, str | None]:
    levels = {m: v for m, v in panel.outcome.get(team, {}).items() if m <= through}
    if not levels:
        return None, None
    g = panel.rollout.get(team)
    pre = [v for m, v in sorted(levels.items()) if g is not None and m < g]
    if pre:
        return _mean(pre), "pre"
    return _mean(list(levels.values())), "all"


def estimate_team(panel: Panel, team: str, through: str, *, min_pre: int = MIN_PRE, min_post: int = MIN_POST,
                  alpha: float = PRETREND_ALPHA) -> TeamResult:
    baseline, basis = _baseline(panel, team, through)
    months = sorted(m for m in panel.y[team] if m <= through)
    g = panel.rollout.get(team)
    if g is None:
        return TeamResult(team, CONTROL, None, len(months), 0, baseline, basis, None)
    n_pre = sum(m < g for m in months)
    n_post = sum(m >= g for m in months)

    def unmeasured(reason: str, analysis: Analysis | None = None) -> TeamResult:
        return TeamResult(team, UNMEASURED, reason, n_pre, n_post, baseline, basis, analysis)

    if n_pre < min_pre:
        return unmeasured(NO_PRE_PERIOD)
    if n_post < min_post:
        return unmeasured(TOO_RECENT)
    analysis = analyse(panel, team, through, alpha=alpha)
    if analysis is None or len(analysis.cells) < min_post:
        return unmeasured(NO_COMPARISON, analysis)
    if not analysis.computable:
        return unmeasured(VARIANCE_NOT_COMPUTABLE, analysis)
    if analysis.pretrend.flagged:
        return unmeasured(PRE_TREND, analysis)
    return TeamResult(team, MEASURED, None, n_pre, len(analysis.cells), baseline, basis, analysis)


def estimate(panel: Panel, through: str, **kwargs) -> dict[str, TeamResult]:
    """Estimate every team in the panel, ordered by team id."""
    return {team: estimate_team(panel, team, through, **kwargs) for team in sorted(panel.y)}


def read_panel(con, through: str) -> Panel:
    """Read the estimator input mart (`fct_team_month`) up to and including `through`.

    A month whose log outcome is undefined is left out of the panel.
    """
    rows = con.execute(
        "SELECT team_id, strftime(month, '%Y-%m'), y_log, outcome, strftime(rollout_month, '%Y-%m')"
        " FROM fct_team_month WHERE strftime(month, '%Y-%m') <= ? AND y_log IS NOT NULL ORDER BY team_id, month",
        [through],
    ).fetchall()
    y: dict[str, dict[str, float]] = {}
    outcome: dict[str, dict[str, float]] = {}
    rollout: dict[str, str | None] = {}
    for team, month, y_log, level, g in rows:
        y.setdefault(team, {})[month] = float(y_log)
        outcome.setdefault(team, {})[month] = float(level)
        rollout[team] = g
    return Panel(y, rollout, outcome)
