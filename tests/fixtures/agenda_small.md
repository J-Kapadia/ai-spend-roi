# agenda_small: hand-worked testing agenda

Fixture `agenda_small.json`, one row per reason a team can be unmeasured, one row per way of
getting the user-month standard deviation sigma, and the `wait` design on both sides of its floor.

## Formulas

With alpha = 0.05 and power 0.80: Z = z(0.975) + z(0.80) = 1.959964 + 0.841621 = 2.801585, so
2 Z^2 = 15.697759. For a test of `L` months with within-user correlation rho = 0.05:

    n(L) = 2 Z^2 sigma^2 (1 + (L - 1) rho) / (L delta^2)

The test length is the smallest L in 1..6 with `ceil(n(L)) <= team_size / 2`; none means
`not_testable_alone`.

- delta, break-even effect, additive: spend per head per month (cents / 100) over the value per
  unit. Team-level: spend of the whole team per month over the value per unit.
- sigma, additive: sd of ln(outcome) over the months used, times the baseline, times sqrt(team size).
  Team-level: the raw sd of the monthly value. The months used are the pre-rollout ones when there
  are at least two, otherwise all of them. `sigma_override` replaces it.

## holdback rows

**np_all** (`no_pre_period`, months used: all six, baseline 20, 100 people). ln of 18, 20, 22, 20, 21, 19 =
2.890372, 2.995732, 3.091042, 2.995732, 3.044522, 2.944439; mean 2.993640; sd 0.070951.
sigma = 0.070951 x 20 x sqrt(100) = 14.1902. delta = 96.00 / 16 = 6.0 per head.
n(1) = 15.697759 x 14.1902^2 / 6^2 = 87.80, ceil 88 > 50. n(2) = 87.80 x 1.05 / 2 = 46.10, ceil 47 <= 50.
Design `holdback`, 47 per arm, 2 months.

**pt_pre** (`pre_trend`, months used: the eight pre months, baseline 50, 60 people). ln of 48, 52, 50, 54,
46, 50, 49, 51 has sd 0.049094; sigma = 0.049094 x 50 x sqrt(60) = 19.0141; delta = 80 / 10 = 8.0.
n(1) = 88.68, n(2) = 46.56, n(3) = 32.51 -> 33 > 30, n(4) = 25.49 -> 26 <= 30.
`holdback`, 26 per arm, 4 months.

**pt_team_level** (`pre_trend`, a duration, 30 people, baseline 12.4 days). Raw sd of 10, 14, 11, 15, 12:
mean 12.4, squared deviations 5.76 + 2.56 + 1.96 + 6.76 + 0.16 = 17.2, variance 17.2 / 4 = 4.3,
sd 2.0736, no sqrt(team size). delta = 40.00 x 30 / 600 = 2.0 days.
n(1) = 15.697759 x 4.3 / 4 = 16.88 -> 17 > 15, n(2) = 16.88 x 1.05 / 2 = 8.86 -> 9 <= 15.
`holdback`, 9 per arm, 2 months.

**np_override** (`no_pre_period`, 80 people, configured sigma 12.0 replaces the books). delta = 60 / 10 = 6.0.
n(1) = 15.697759 x 144 / 36 = 62.79 -> 63 > 40, n(2) = 62.79 x 1.05 / 2 = 32.97 -> 33 <= 40.
`holdback`, 33 per arm, 2 months.

**np_small** (`no_pre_period`, 20 people). ln of 2, 9, 3, 12 has sd 0.858709; sigma = 0.858709 x 6.5 x sqrt(20) = 24.96;
delta = 30 / 10 = 3.0. n(1) = 1086.79, and even n(6) = 226.4 -> 227 is far above 10 (half the team).
`not_testable_alone`: pair with another team on a staggered start.

**vnc** (`variance_not_computable`: no interval could be formed, so the comparison is missing like
a pre-trend; 50 people, baseline 25). sd of ln(24, 26, 23, 27, 25) = 0.063378; sigma = 0.063378 x 25 x sqrt(50) = 11.2038;
delta = 50 / 10 = 5.0. n(3) = 28.90 -> 29 > 25, n(4) = 22.66 -> 23 <= 25. `holdback`, 23 per arm, 4 months.

## pair_stagger row

**nc_pair** (`no_comparison`, 30 people paired with a 50-person team: combined headcount 80, half 40).
ln of 100, 104, 98, 102 has sd 0.025572. sigma comes from the team's own books and own size:
0.025572 x 101 x sqrt(30) = 14.1465. The pair's combined headcount enters only as the capacity
limit (half of 80 = 40, where alone half of 30 = 15). delta = 90 / 9 = 10.0.
n(1) = 15.697759 x 14.1465^2 / 100 = 31.41 -> 32 <= 40.
`pair_stagger`, 32 per arm, 1 month, the second team starting 3 months after the first.

## wait rows (`too_recent`)

The interval of a team with `n` post-rollout months has standard error `sd sqrt((n + 1) / (2 n))`, where
`sd` is the root of the mean of its cell variances. It falls with `n` but never below `sd / sqrt(2)`
because every cell shares the base month. With `df` degrees of freedom the interval half-width is
`t sd sqrt((n + 1) / (2 n))`; the smallest `m` in 1..12 with n = n_post + m such that this is at most
the break-even effect in log points.

**tr_reach** (higher is better, baseline 40, n_post 3, df 2, sd = sqrt(0.0004) = 0.02). t(0.975, 2) = 4.302653.
delta = 56.00 / 20 = 2.8; delta_log = ln(1 + 2.8 / 40) = 0.067659. Floor t sd / sqrt(2) = 0.060849 < 0.067659.
m = 1 (n = 4): 4.302653 x 0.02 x sqrt(5 / 8) = 0.068031 > 0.067659. m = 2 (n = 5): 0.086053 x sqrt(6 / 10) = 0.066656 <= 0.067659.
`wait`, 2 months (delta and sigma stored in log points: 0.067659 and 0.02).

**tr_floor** (lower is better, baseline 30, n_post 3, df 2, sd = sqrt(0.0025) = 0.05). delta = 60 / 20 = 3.0;
delta_log = -ln(1 - 3 / 30) = 0.105361. Floor t sd / sqrt(2) = 4.302653 x 0.05 / 1.414214 = 0.152122 > 0.105361:
no number of months gets there. `not_testable_alone`, with the note that the base-month noise does not shrink with waiting.

**tr_far** (as tr_reach but delta = 51.00 / 20 = 2.55, delta_log = ln(1 + 2.55 / 40) = 0.061800). The floor 0.060849 is
below the target, but even at m = 12 (n = 15) the half-width is 0.062844 > 0.061800 (it needs about 200 months).
`not_testable_alone`: waiting 12 months does not reach the break-even effect.
