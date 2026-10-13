# panel_small: hand-worked derivation

Fixture `panel_small.csv`. Every number below is exact rational arithmetic on the three-decimal `y_log` column (`Y = ln(outcome)`, direction +1), shown to six places; `panel_small.expected.json` holds the same values and `test_estimator.py` compares the estimator, and the independent loops in `tests/reference/cs_reference.py`, to them to 1e-6. Months are written m1 to m10 for 2025-01 to 2025-10.

## Design

Five teams, ten months. `team_a`, `team_b` and `team_c` first have spend in m4, m5 and m6 (cohorts g = 4, 5, 6): 3, 4 and 5 pre-period months and 7, 6 and 5 post-period months. `team_n1` and `team_n2` are never treated. `Y = base + shock + effect + wobble`, with a common month shock (0, .03, -.02, .05, .01, -.03, .04, 0, .02, -.01 for m1 to m10), a planted effect that grows with time since rollout (`0.04k`, `0.025k`, `0.01k` for k = 1, 2, … months since rollout; teams that roll out early gain most and keep gaining) and a hand-rounded wobble of about ±0.01.

The bases are 6.91, 3.40, 1.10, 4.20 and 0.00 in log points, so `team_a` reports about 1,000 outcome units a head and `team_n2` about 1: two teams a thousand times apart in levels (outcome column). That is the two-scale part of the fixture.

## The data (`y_log`)

| team | m1 | m2 | m3 | m4 | m5 | m6 | m7 | m8 | m9 | m10 |
|---|---|---|---|---|---|---|---|---|---|---|
| team_a | 6.913 | 6.948 | 6.893 | 6.987 | 7.009 | 7.004 | 7.105 | 7.116 | 7.174 | 7.183 |
| team_b | 3.400 | 3.435 | 3.373 | 3.448 | 3.430 | 3.426 | 3.515 | 3.497 | 3.537 | 3.537 |
| team_c | 1.100 | 1.127 | 1.093 | 1.160 | 1.083 | 1.061 | 1.158 | 1.126 | 1.162 | 1.142 |
| team_n1 | 4.221 | 4.219 | 4.176 | 4.270 | 4.216 | 4.177 | 4.235 | 4.184 | 4.222 | 4.191 |
| team_n2 | -0.012 | 0.023 | -0.021 | 0.041 | 0.009 | -0.029 | 0.040 | -0.005 | 0.026 | -0.001 |

## One cell by hand: ATT(g = m4, t = m4) for `team_a`

Pre-period S = {m1, m2, m3}, base month b = m3. Controls valid at t = m4 are the teams with no spend through max(t, b) = m4: `team_b` (g = m5), `team_c` (g = m6), `team_n1` and `team_n2`, four of them.

Backcast d_s = Y(team_a, s) − mean of the four controls at s:

| s | Y(team_a) | b | c | n1 | n2 | control mean | d_s |
|---|---|---|---|---|---|---|---|
| m1 | 6.913 | 3.400 | 1.100 | 4.221 | -0.012 | 2.17725 | 4.73575 |
| m2 | 6.948 | 3.435 | 1.127 | 4.219 | 0.023 | 2.201 | 4.747 |
| m3 | 6.893 | 3.373 | 1.093 | 4.176 | -0.021 | 2.15525 | 4.73775 |
| m4 | 6.987 | 3.448 | 1.160 | 4.270 | 0.041 | 2.22975 | 4.75725 |

ATT(m4, m4) = d_m4 − d_m3 = 4.75725 − (4.73775) = **0.0195**.

Its variance is the sample variance (n − 1) of the one-period changes of d over S: changes d_m2 − d_m1 = 0.01125 and d_m3 − d_m2 = -0.00925; mean 0.001; squared deviations sum to 0.000210125; divided by n − 1 = 1 gives **Var = 0.000210125**, standard error 0.014496.

## A pair of cells whose control sets differ

At t = m5 `team_b` is treated (g = m5 is not later than t), so the controls of ATT(m4, m5) are `team_c`, `team_n1`, `team_n2` (three), while those of ATT(m4, m4) were four. The backcast series differ, so each cell has its own variance and the covariance is taken between the two level series over S:

| s | d^(m4)_s (four controls) | d^(m5)_s (three controls) |
|---|---|---|
| m1 | 4.73575 | ≈ 5.143333 |
| m2 | 4.747 | ≈ 5.158333 |
| m3 | 4.73775 | ≈ 5.143667 |

ATT(m4, m5) = d^(m5)_m5 − d^(m5)_m3 = **0.096**, Var = **≈ 0.000440** (changes 0.015, ≈ -0.014667). Cov = sample covariance (n − 1 = 2) of the two level series = **≈ 0.000051**; Cov / √(Var₁Var₂) = 0.167; with one control set serving both cells it would be about 1/2 in expectation, and a cell with fewer controls carries more control noise, hence the larger variance.

## Team effects

### team_a

Cohort g = m4, pre-period n_pre = 3, post cells n_post = 7, df = n_pre − 2 = 1, t(0.975, 1) = 12.706205.

| t | controls | ATT(g, t) | Var | SE |
|---|---|---|---|---|
| m4 | 4 | 0.019500 | 0.00021012 | 0.014496 |
| m5 | 3 | 0.096000 | 0.00044006 | 0.020978 |
| m6 | 2 | 0.114500 | 0.00045000 | 0.021213 |
| m7 | 2 | 0.152000 | 0.00045000 | 0.021213 |
| m8 | 2 | 0.211000 | 0.00045000 | 0.021213 |
| m9 | 2 | 0.234500 | 0.00045000 | 0.021213 |
| m10 | 2 | 0.272500 | 0.00045000 | 0.021213 |

Σ (cells in the order above; variances on the diagonal, sample covariances of the backcast level series off it):

```
0.00021012  0.00005085  0.00005475  0.00005475  0.00005475  0.00005475  0.00005475
0.00005085  0.00044006  0.00007475  0.00007475  0.00007475  0.00007475  0.00007475
0.00005475  0.00007475  0.00045000  0.00008725  0.00008725  0.00008725  0.00008725
0.00005475  0.00007475  0.00008725  0.00045000  0.00008725  0.00008725  0.00008725
0.00005475  0.00007475  0.00008725  0.00008725  0.00045000  0.00008725  0.00008725
0.00005475  0.00007475  0.00008725  0.00008725  0.00008725  0.00045000  0.00008725
0.00005475  0.00007475  0.00008725  0.00008725  0.00008725  0.00008725  0.00045000
```

Team effect = mean of the 7 ATT = **0.157143** (exactly 11/70). With w = 1/7, Var = wᵀΣw = 0.0001233036 (exactly 1381/11200000), SE = 0.011104. Interval 0.157143 ± 12.706205 × 0.011104 = **[0.016050, 0.298235]**.

Pre-period leads (t = m1, m2): ATT = -0.002000, 0.009250; the same control set serves every lead, so Σ_pre has the variance of the changes of d on the diagonal and the variance of the levels of d off it: [[0.00021012, 3.602e-05], [3.602e-05, 0.00021012]]. The Wald statistic aᵀΣ⁻¹a / L = **0.235119** against F(L = 2, df = 1), p = 0.824719, above 10%, so the team is not flagged.

### team_b

Cohort g = m5, pre-period n_pre = 4, post cells n_post = 6, df = n_pre − 2 = 2, t(0.975, 2) = 4.302653.

| t | controls | ATT(g, t) | Var | SE |
|---|---|---|---|---|
| m5 | 3 | 0.036333 | 0.00034144 | 0.018478 |
| m6 | 2 | 0.059500 | 0.00034525 | 0.018581 |
| m7 | 2 | 0.085000 | 0.00034525 | 0.018581 |
| m8 | 2 | 0.115000 | 0.00034525 | 0.018581 |
| m9 | 2 | 0.120500 | 0.00034525 | 0.018581 |
| m10 | 2 | 0.149500 | 0.00034525 | 0.018581 |

Σ (cells in the order above; variances on the diagonal, sample covariances of the backcast level series off it):

```
0.00034144  0.00009549  0.00009549  0.00009549  0.00009549  0.00009549
0.00009549  0.00034525  0.00009706  0.00009706  0.00009706  0.00009706
0.00009549  0.00009706  0.00034525  0.00009706  0.00009706  0.00009706
0.00009549  0.00009706  0.00009706  0.00034525  0.00009706  0.00009706
0.00009549  0.00009706  0.00009706  0.00009706  0.00034525  0.00009706
0.00009549  0.00009706  0.00009706  0.00009706  0.00009706  0.00034525
```

Team effect = mean of the 6 ATT = **0.094306** (exactly 679/7200). With w = 1/6, Var = wᵀΣw = 0.0001378835 (exactly 178697/1296000000), SE = 0.011742. Interval 0.094306 ± 4.302653 × 0.011742 = **[0.043782, 0.144829]**.

Pre-period leads (t = m1, m2, m3): ATT = 0.006000, 0.021000, -0.000667; the same control set serves every lead, so Σ_pre has the variance of the changes of d on the diagonal and the variance of the levels of d off it: [[0.00034144, 0.00010136, 0.00010136], [0.00010136, 0.00034144, 0.00010136], [0.00010136, 0.00010136, 0.00034144]]. The Wald statistic aᵀΣ⁻¹a / L = **0.483551** against F(L = 3, df = 2), p = 0.727420, above 10%, so the team is not flagged.

### team_c

Cohort g = m6, pre-period n_pre = 5, post cells n_post = 5, df = n_pre − 2 = 3, t(0.975, 3) = 3.182446.

| t | controls | ATT(g, t) | Var | SE |
|---|---|---|---|---|
| m6 | 2 | 0.016500 | 0.00044042 | 0.020986 |
| m7 | 2 | 0.050000 | 0.00044042 | 0.020986 |
| m8 | 2 | 0.066000 | 0.00044042 | 0.020986 |
| m9 | 2 | 0.067500 | 0.00044042 | 0.020986 |
| m10 | 2 | 0.076500 | 0.00044042 | 0.020986 |

Σ (cells in the order above; variances on the diagonal, sample covariances of the backcast level series off it):

```
0.00044042  0.00029355  0.00029355  0.00029355  0.00029355
0.00029355  0.00044042  0.00029355  0.00029355  0.00029355
0.00029355  0.00029355  0.00044042  0.00029355  0.00029355
0.00029355  0.00029355  0.00029355  0.00044042  0.00029355
0.00029355  0.00029355  0.00029355  0.00029355  0.00044042
```

Team effect = mean of the 5 ATT = **0.055300** (exactly 553/10000). With w = 1/5, Var = wᵀΣw = 0.0003229233 (exactly 96877/300000000), SE = 0.017970. Interval 0.055300 ± 3.182446 × 0.017970 = **[-0.001889, 0.112489]**.

Pre-period leads (t = m1, m2, m3, m4): ATT = 0.025000, 0.035500, 0.045000, 0.034000; the same control set serves every lead, so Σ_pre has the variance of the changes of d on the diagonal and the variance of the levels of d off it: [[0.00044042, 0.00029355, 0.00029355, 0.00029355], [0.00029355, 0.00044042, 0.00029355, 0.00029355], [0.00029355, 0.00029355, 0.00044042, 0.00029355], [0.00029355, 0.00029355, 0.00029355, 0.00044042]]. The Wald statistic aᵀΣ⁻¹a / L = **1.263136** against F(L = 4, df = 3), p = 0.441414, above 10%, so the team is not flagged.

## Status-only expectations

The fixture has no `too_recent` cohort when read through m10, so those expectations come from truncating it, and are status only (no figures): read through m6, `team_a` has three post-period months (measured) while `team_b` (two) and `team_c` (one) are `too_recent`; read from m3 on (months m1 and m2 dropped), `team_a` (one pre-period month) and `team_b` (two) are `no_pre_period` and `team_c` (three) is measured. The never-treated teams are `control` throughout.

## Why a two-way fixed-effects fit is wrong here

The planted effect averaged over the 18 treated team-months is 0.099722. The two-way fixed-effects coefficient on a treated indicator is 0.062828, 37.0% below it: already-treated teams, whose effects are still growing, serve as controls for the later cohorts. The group-time estimator above uses only teams not yet treated and never-treated teams as controls; its team effects (0.157143, 0.094306, 0.055300) sit within about 1.5 standard errors of the planted team averages (0.1600, 0.0875, 0.0300).

## Two scales

`team_a` is reported near 1,000 units a head and `team_n2` near 1. Differences of the log outcome cancel those scales, so rescaling any team's outcome by a constant (adding ln of the constant to its Y) leaves every ATT unchanged; a difference of levels would put the thousand-fold gap straight into `team_a`'s ATT.

## panel_pretrend.csv

Four teams, ten months: `team_x` (g = m6), `team_p` (g = m7), `team_n1`, `team_n2` (never treated). `team_x` drifts upward by 0.05 a month relative to the others before its rollout (its Y in m1 to m4 is lowered by 0.20, 0.15, 0.10, 0.05 and is unchanged in m5, the base month), on top of the same kind of wobble as above (sd 0.03). Its four pre-period leads against the three valid controls (`team_p`, `team_n1`, `team_n2`) are -0.256667, -0.247333, -0.141333, -0.190667; Σ_pre is positive definite, the Wald statistic aᵀΣ⁻¹a / 4 is 7.899295 against F(4, 3) whose 90% point is 5.342644 (p = 0.060514), so `team_x` is `unmeasured` with reason `pre_trend` and has no effect or interval. `team_p` (p = 0.550) stays `measured`. A clean linear drift would make Σ_pre non-positive-definite (the variance of the levels of d would exceed the variance of its changes) and the test untestable, so the planted drift here is a drift with enough wobble for Σ_pre to be positive definite.

## panel_nocomparison.csv

Three teams, eight months, g = m4, m5, m6, all treated by the last month (m8) and none never-treated. A control for ATT(g, t) must have no spend through max(t, g − 1): for `team_p` that leaves `team_q` at m4 and `team_r` at m4 and m5, and nobody from m6 on, so two valid cells (fewer than min_post = 3); `team_q` has one (m5, with `team_r`) and `team_r` none. Each has at least three post-period months (so they are not `too_recent`) and at least three pre-period months, so all three are `unmeasured` with reason `no_comparison`.
