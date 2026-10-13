"""Dose-response curve per measured team: effect(x) = A (1 - exp(-x / k)), x in cents of spend
per head per month, the effect in log points.

The curve is fitted to the team's group-time effects ATT(g, t) against the spend per head of
the same months by generalised least squares with the team's full covariance Sigma of those
cells (`est_att_cov`). The cells share a base month, so their errors are correlated; weighting
them as independent mis-states the standard error of the marginal return. When the
estimated Sigma is singular (a sample covariance over few pre-period months has rank at most
n_pre - 1) the fit uses the covariance the estimator's own formula gives for cells that share
their controls instead: the same variances and a correlation of one half.

`k` is bounded to [0.25 x the smallest, 8 x the largest] positive spend per head among the
cells. For fixed `k` the model is linear in `A`, so the fit profiles `A` out and searches `k`
on a log grid, then refines it. The curve is `identified` when A > 0, `k` lies strictly inside
its bounds and the one-sided 95% lower bound of the marginal return at the team's current
spend, by the delta method, is above zero. A team with fewer than three cells is not fitted.

Pure functions plus two readers of the warehouse; nothing here reads the truth file.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from scipy import optimize, stats

K_LOW = 0.25
K_HIGH = 8.0
GRID = 240
MIN_CELLS = 3
CURRENT_MONTHS = 3  # the current spend is the mean of the last three months
ONE_SIDED = 0.95
BOUND_TOL = 1e-6  # relative distance from a bound of k that still counts as at the bound


@dataclass(frozen=True)
class Fit:
    a: float | None
    k: float | None
    identified: bool
    marginal_at_current: float | None  # log points per cent of spend per head, at x0
    marginal_se: float | None
    reason: str  # why the curve is not identified; empty when it is


def effect(a: float, k: float, x: float) -> float:
    return a * (1.0 - math.exp(-x / k))


def marginal(a: float, k: float, x: float) -> float:
    """Derivative of the effect with respect to spend per head."""
    return a / k * math.exp(-x / k)


def usable_covariance(cov: np.ndarray) -> np.ndarray:
    """`cov` when it is positive definite, otherwise the equicorrelated stand-in (rho = 1/2)."""
    n = cov.shape[0]
    sd = np.sqrt(np.clip(np.diag(cov), 0.0, None))
    eig = np.linalg.eigvalsh(cov)
    if np.all(sd > 0.0) and eig.min() > 1e-12 * max(abs(eig.max()), 1e-300):
        return cov
    stand_in = 0.5 * np.outer(sd, sd)
    np.fill_diagonal(stand_in, sd**2)
    return stand_in


def _profile(k: float, x: np.ndarray, y: np.ndarray, p: np.ndarray) -> tuple[float, float]:
    """For a given k: the best A and the weighted sum of squares."""
    phi = 1.0 - np.exp(-x / k)
    denom = float(phi @ p @ phi)
    if denom <= 0.0:
        return math.nan, math.inf
    a = float(phi @ p @ y) / denom
    r = y - a * phi
    return a, float(r @ p @ r)


def fit(x: np.ndarray, y: np.ndarray, cov: np.ndarray, x0: float) -> Fit:
    """Fit the curve to effects `y` observed at spends `x` with covariance `cov`; `x0` is the
    current spend per head at which the marginal return is judged."""
    n = len(x)
    if n < MIN_CELLS:
        return Fit(None, None, False, None, None, "fewer than three cells")
    positive = x[x > 0.0]
    if positive.size == 0:
        return Fit(None, None, False, None, None, "no spend in any cell")
    k_lo, k_hi = K_LOW * float(positive.min()), K_HIGH * float(positive.max())
    p = np.linalg.inv(usable_covariance(cov))
    p = (p + p.T) / 2.0

    grid = np.geomspace(k_lo, k_hi, GRID)
    sums = np.array([_profile(float(k), x, y, p)[1] for k in grid])
    if not np.isfinite(sums).any():
        return Fit(None, None, False, None, None, "the curve cannot be fitted")
    i = int(np.argmin(sums))
    lo, hi = grid[max(i - 1, 0)], grid[min(i + 1, GRID - 1)]
    if hi > lo:
        res = optimize.minimize_scalar(
            lambda lk: _profile(math.exp(lk), x, y, p)[1], bounds=(math.log(lo), math.log(hi)),
            method="bounded", options={"xatol": 1e-12},
        )
        k = math.exp(float(res.x))
        if _profile(k, x, y, p)[1] > sums[i]:  # the refinement never loses to the grid point
            k = float(grid[i])
    else:
        k = float(grid[i])
    a, _ = _profile(k, x, y, p)

    interior = k_lo * (1.0 + BOUND_TOL) < k < k_hi * (1.0 - BOUND_TOL)
    m = marginal(a, k, x0)
    # Delta method: Cov(theta) = (J' P J)^-1 with J the Jacobian of the curve at the fit.
    e = np.exp(-x / k)
    jac = np.column_stack([1.0 - e, -a * x / k**2 * e])
    info = jac.T @ p @ jac
    se = None
    if np.linalg.matrix_rank(info) == 2:
        cov_theta = np.linalg.inv(info)
        grad = np.array([math.exp(-x0 / k) / k, a / k**2 * math.exp(-x0 / k) * (x0 / k - 1.0)])
        var = float(grad @ cov_theta @ grad)
        se = math.sqrt(var) if var > 0.0 else None

    reason = ""
    if a <= 0.0:
        reason = "the effect does not rise with spend"
    elif not interior:
        reason = "k is at a bound of the observed range"
    elif se is None:
        reason = "the marginal return has no standard error"
    else:
        t = float(stats.t.ppf(ONE_SIDED, n - 2))
        if not m - t * se > 0.0:
            reason = "the marginal return at current spend is not above zero"
    return Fit(a, k, reason == "", m, se, reason)


def spend_profile(con, through: str) -> dict[str, tuple[float, float, int]]:
    """Per team up to `through`: current spend per head (the mean of the last three months),
    the highest spend per head observed, and the latest headcount. Cents per head per month."""
    rows = con.execute(
        "SELECT team_id, spend_per_head_cents, headcount FROM fct_team_month"
        " WHERE strftime(month, '%Y-%m') <= ? ORDER BY team_id, month",
        [through],
    ).fetchall()
    by_team: dict[str, list[tuple[float, int]]] = {}
    for team, spend, heads in rows:
        by_team.setdefault(team, []).append((float(spend), int(heads)))
    out = {}
    for team, series in by_team.items():
        last = [s for s, _ in series[-CURRENT_MONTHS:]]
        out[team] = (sum(last) / len(last), max(s for s, _ in series), series[-1][1])
    return out


def read_cells(con, through: str, team: str) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """The team's cells at `through`: spend per head, effect and covariance, ordered by month."""
    cells = con.execute(
        "SELECT g.t, g.att, f.spend_per_head_cents FROM est_att_gt g"
        " JOIN fct_team_month f ON f.team_id = g.team_id AND f.month = g.t"
        " WHERE g.through_month = ? AND g.team_id = ? ORDER BY g.t",
        [through, team],
    ).fetchall()
    months = [c[0] for c in cells]
    index = {m: i for i, m in enumerate(months)}
    cov = np.zeros((len(months), len(months)))
    for t1, t2, c in con.execute(
        "SELECT t1, t2, cov FROM est_att_cov WHERE through_month = ? AND team_id = ?", [through, team]
    ).fetchall():
        cov[index[t1], index[t2]] = cov[index[t2], index[t1]] = c
    return np.array([c[2] for c in cells], dtype=float), np.array([c[1] for c in cells], dtype=float), cov


def fit_team(con, through: str, team: str, x0: float) -> Fit:
    x, y, cov = read_cells(con, through, team)
    return fit(x, y, cov, x0)
