# alloc_small: hand-worked allocation

Fixture `alloc_small.json`: three measured teams, a monthly increment of $50,000 = 5,000,000 cents.
Spend `x` is cents per head per month. Each team's curve is `effect(x) = A (1 - exp(-x / k))`
(log points). Every team sits at `x0 = k = max observed spend`, so it is at 63% of its asymptote,
and its cap is 1.5 x the maximum observed spend.

| team | kind | heads h | baseline b | A | k = x0 = max | value per unit v | cap |
|---|---|---|---|---|---|---|---|
| finance | team_level | 40 | 12 | 0.15 | 10,000 | $9,000 | 15,000 |
| sales | additive | 250 | 25 | 0.30 | 30,000 | $35 | 45,000 |
| support | additive | 300 | 10 | 0.24 | 20,000 | $60 | 30,000 |

## Marginal value per cent

The first-order monthly value of a team is `v h b effect(x)` for an additive metric and `v b effect(x)`
for a team-level one (a duration is not a sum over people). A cent per head costs `h` cents, so the
value per extra cent of total spend is `w A/k exp(-x/k)` with `w = v b` (additive) or `v b / h`
(team-level):

- finance: w = 9000 x 12 / 40 = 2700, c = w A / k = 2700 x 0.15 / 10000 = 0.0405
- sales: w = 35 x 25 = 875, c = 875 x 0.30 / 30000 = 0.00875
- support: w = 60 x 10 = 600, c = 600 x 0.24 / 20000 = 0.0072

At `x0` (`exp(-1) = 0.367879`) the marginal return per $1 of spend is 100 c exp(-1): finance 1.4899,
sales 0.3219, support 0.2649.

## Equal marginals

A funded, uncapped team satisfies `c exp(-x/k) = lambda`, i.e. `x = k (ln c - ln lambda)`.

Finance has by far the highest marginal return, so it is pushed to its cap. Its headroom is
h (cap - x0) = 40 x 5,000 = 200,000 cents. The rest, 5,000,000 - 200,000 = 4,800,000 cents, goes to
sales and support, with `sum h (x - x0) = 4,800,000`:

    sum h k ln c - ln(lambda) sum h k - sum h x0 = 4,800,000

- sum h k = 250 x 30,000 + 300 x 20,000 = 7,500,000 + 6,000,000 = 13,500,000
- sum h x0 = 250 x 30,000 + 300 x 20,000 = 13,500,000
- ln c: sales ln 0.00875 = -4.738702, support ln 0.0072 = -4.933674
- sum h k ln c = 7,500,000 x (-4.738702) + 6,000,000 x (-4.933674) = -35,540,262 - 29,602,046 = -65,142,308

    ln(lambda) = (-65,142,308 - 13,500,000 - 4,800,000) / 13,500,000 = -6.180912, lambda = 0.00206854

(0.2068541 per $1 of spend.) Then:

- sales: x = 30,000 x (-4.738702 + 6.180912) = 43,266.30, below its cap of 45,000;
  increment 250 x (43,266.30 - 30,000) = 3,316,575.58 cents
- support: x = 20,000 x (-4.933674 + 6.180912) = 24,944.75, below its cap of 30,000;
  increment 300 x (24,944.75 - 20,000) = 1,483,424.42 cents
- finance, unconstrained: x = 10,000 x (ln 0.0405 + 6.180912) = 29,745, above its cap, so the cap
  binds. At the cap its marginal return is 100 x 0.0405 x exp(-1.5) = 0.9037 per $1, above lambda,
  so capping it is right.

## Rounding to cents

Floors: 3,316,575 + 1,483,424 = 4,799,999 for the two uncapped teams, one cent short of 4,800,000.
The fractions are 0.58 (sales) and 0.42 (support), so the extra cent goes to sales (largest
remainder), and the capped team keeps its exact 200,000.

| team | amount (cents) | amount | capped |
|---|---|---|---|
| finance | 200,000 | $2,000.00 | yes |
| sales | 3,316,576 | $33,165.76 | no |
| support | 1,483,424 | $14,834.24 | no |

Sum 5,000,000 cents = the budget, unallocated 0. The marginal returns of sales and support at
`x0` plus the increment are equal (0.2068541 per $1); finance is capped and not part of that
equality.
