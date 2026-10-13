"""Independent reference for the group-time estimator: plain loops from the formulas.

Imports nothing from `roi`. Inputs are plain dicts: `y[team][month]` (the log outcome, months as
`YYYY-MM` strings) and `rollout[team]` (first month with spend, or None). The only library
calls are the Student-t and F quantile/tail functions.

Formulas (log outcome Y; team i, cohort g, pre-period months S, base month b = last of S):

    for a cell (g, t), C_t = the controls c with rollout[c] None or > max(t, b) that are
    observed in every month of S and in t
    d_s     = Y[i,s] - (1/|C_t|) sum_c Y[c,s]                     s in S and s = t
    ATT     = d_t - d_b
    Var     = sample variance (n-1) of the one-period changes d_s - d_(s-1), s in S
    Cov     = sample covariance (n-1) of the series d^(t)_s and d^(t')_s over S
    effect  = mean of ATT over the valid post cells, variance w' Sigma w with w = 1/n_post
    df      = |S| - 2, interval effect +- t_(0.975, df) sqrt(variance)
    pre-trend: leads a_t = d^(t)_t - d^(t)_b for t in S except b; Wald a' Sigma_pre^-1 a / L
    against F(L, df); Sigma_pre built like Sigma from the lead cells
"""

from __future__ import annotations

import math

from scipy import stats


def mean(xs):
    total = 0.0
    for x in xs:
        total += x
    return total / len(xs)


def sample_cov(xs, ys):
    mx, my = mean(xs), mean(ys)
    total = 0.0
    for a, b in zip(xs, ys):
        total += (a - mx) * (b - my)
    return total / (len(xs) - 1)


def valid_controls(y, rollout, team, t, pre):
    base = pre[-1]
    horizon = t if t > base else base
    found = []
    for c in sorted(y):
        if c == team:
            continue
        if rollout[c] is not None and rollout[c] <= horizon:
            continue
        if t not in y[c]:
            continue
        if any(s not in y[c] for s in pre):
            continue
        found.append(c)
    return found


def backcast(y, team, controls, months):
    d = {}
    for s in months:
        total = 0.0
        for c in controls:
            total += y[c][s]
        d[s] = y[team][s] - total / len(controls)
    return d


def one_cell(y, rollout, team, t, pre):
    controls = valid_controls(y, rollout, team, t, pre)
    if not controls:
        return None
    months = list(pre) if t in pre else list(pre) + [t]
    d = backcast(y, team, controls, months)
    levels = [d[s] for s in pre]
    changes = [levels[k] - levels[k - 1] for k in range(1, len(levels))]
    return {
        "t": t,
        "att": d[t] - d[pre[-1]],
        "var": sample_cov(changes, changes),
        "n_controls": len(controls),
        "controls": controls,
        "levels": levels,
    }


def sigma(cells):
    n = len(cells)
    out = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(n):
            if i == j:
                out[i][j] = cells[i]["var"]
            else:
                out[i][j] = sample_cov(cells[i]["levels"], cells[j]["levels"])
    return out


def cholesky_ok(m):
    """True when the symmetric matrix is positive definite (plain Cholesky)."""
    n = len(m)
    low = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(i + 1):
            s = m[i][j]
            for k in range(j):
                s -= low[i][k] * low[j][k]
            if i == j:
                if s <= 1e-12 * max(abs(m[r][r]) for r in range(n)):
                    return False
                low[i][i] = math.sqrt(s)
            else:
                low[i][j] = s / low[j][j]
    return True


def solve(m, b):
    """Gauss-Jordan solve of m x = b."""
    n = len(m)
    a = [list(m[i]) + [b[i]] for i in range(n)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(a[r][col]))
        a[col], a[pivot] = a[pivot], a[col]
        p = a[col][col]
        a[col] = [v / p for v in a[col]]
        for r in range(n):
            if r != col:
                f = a[r][col]
                a[r] = [v - f * w for v, w in zip(a[r], a[col])]
    return [a[i][n] for i in range(n)]


def reference_team(y, rollout, team, through, min_pre=3, min_post=3, alpha=0.10):
    g = rollout[team]
    months = sorted(m for m in y[team] if m <= through)
    if g is None:
        return {"status": "control", "reason": None}
    pre = [m for m in months if m < g]
    post = [m for m in months if m >= g]
    if len(pre) < min_pre:
        return {"status": "unmeasured", "reason": "no_pre_period", "n_pre": len(pre), "n_post": len(post)}
    if len(post) < min_post:
        return {"status": "unmeasured", "reason": "too_recent", "n_pre": len(pre), "n_post": len(post)}
    cells = [c for c in (one_cell(y, rollout, team, t, pre) for t in post) if c is not None]
    if len(cells) < min_post:
        return {"status": "unmeasured", "reason": "no_comparison", "n_pre": len(pre), "n_post": len(cells)}

    n = len(cells)
    cov = sigma(cells)
    effect = mean([c["att"] for c in cells])
    variance = 0.0
    for i in range(n):
        for j in range(n):
            variance += cov[i][j] / (n * n)
    df = len(pre) - 2
    tq = float(stats.t.ppf(0.975, df))
    if variance <= 0.0:
        return {"status": "unmeasured", "reason": "variance_not_computable", "n_pre": len(pre), "n_post": n}
    se = math.sqrt(variance)

    leads = [c for c in (one_cell(y, rollout, team, t, pre) for t in pre[:-1]) if c is not None]
    stat = p = None
    testable = False
    if len(leads) >= 2:
        cov_pre = sigma(leads)
        if cholesky_ok(cov_pre):
            a = [c["att"] for c in leads]
            x = solve(cov_pre, a)
            stat = sum(ai * xi for ai, xi in zip(a, x)) / len(leads)
            p = float(stats.f.sf(stat, len(leads), df))
            testable = True
    flagged = testable and p < alpha
    return {
        "status": "unmeasured" if flagged else "measured",
        "reason": "pre_trend" if flagged else None,
        "n_pre": len(pre),
        "n_post": n,
        "cells": cells,
        "cov": cov,
        "effect": effect,
        "variance": variance,
        "se": se,
        "df": df,
        "t_mult": tq,
        "ci": (effect - tq * se, effect + tq * se),
        "lead_att": [c["att"] for c in leads],
        "lead_stat": stat,
        "lead_p": p,
        "lead_testable": testable,
    }


def reference(y, rollout, through, **kwargs):
    return {team: reference_team(y, rollout, team, through, **kwargs) for team in sorted(y)}


def twfe_coefficient(y, rollout):
    """The naive two-way fixed-effects coefficient on a treated indicator: a diagnostic only.

    Least squares of Y on team dummies, month dummies and D = 1 when the team is treated that
    month, by the Frisch-Waugh route: residualise D on the two sets of dummies (a balanced
    panel, so the double-demeaned D), then regress Y on it.
    """
    teams = sorted(y)
    months = sorted(next(iter(y.values())))
    d = {i: {t: 1.0 if rollout[i] is not None and t >= rollout[i] else 0.0 for t in months} for i in teams}
    grand = mean([d[i][t] for i in teams for t in months])
    row = {i: mean([d[i][t] for t in months]) for i in teams}
    col = {t: mean([d[i][t] for i in teams]) for t in months}
    num = den = 0.0
    for i in teams:
        for t in months:
            dd = d[i][t] - row[i] - col[t] + grand
            num += dd * y[i][t]
            den += dd * dd
    return num / den
