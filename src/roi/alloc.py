"""Allocate a monthly budget increment across measured teams by equalising marginal returns.

Maximise  sum_i  v_i h_i b_i A_i (1 - exp(-x_i / k_i))  over the teams with an identified
curve, where v_i is the value per unit, h_i the headcount for an `additive` metric and 1 for a
`team_level` one, and b_i the baseline outcome (the first-order lift), subject to

    x_i >= x0_i                  no cuts; x0_i is the mean of the last three months
    x_i <= cap_i = 1.5 x the highest spend per head observed
    sum_i headcount_i (x_i - x0_i) = budget

Every x is cents of spend per head per month. With w_i = v_i b_i (additive) or v_i b_i / h_i
(team_level) the first-order condition of a funded, uncapped team is

    w_i (A_i / k_i) exp(-x_i / k_i) = lambda,     lambda = dollars of value per extra cent,

so x_i(lambda) = clip(k_i ln(w_i A_i / (k_i lambda)), x0_i, cap_i) falls as lambda rises and the
budget pins lambda by bisection on its logarithm. A team whose marginal return at x0 is below
lambda gets nothing. Real-valued amounts are rounded to cents by largest remainder so that
they sum exactly to the budget, or, when every cap binds first, to the sum of the caps with the
rest reported as unallocated.

The same solver runs 1,000 seeded draws of each team's value per unit, uniform between its low
and high, to report the share of draws in which a team is funded.
"""

from __future__ import annotations

import json
import math
import zlib
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import yaml

from roi import curve
from roi.workdir import RoiError, WorkDir

CAP_FACTOR = 1.5
DRAWS = 1000
VALUE_DRAW_SEED = 20261013  # fixed, so the draws do not depend on the work dir or the team order
FUNDED_MIN_CENTS = 0.5  # a team is funded when its real increment would round to a cent or more
BISECTIONS = 200
ADDITIVE, TEAM_LEVEL = "additive", "team_level"


@dataclass(frozen=True)
class Team:
    team_id: str
    headcount: int
    kind: str  # additive | team_level
    baseline: float  # outcome per head per month (additive) or the raw monthly level (team_level)
    a: float | None
    k: float | None
    identified: bool
    x0: float  # current spend per head, cents per month
    max_observed: float
    value: float  # dollars per unit, base
    value_low: float
    value_high: float

    @property
    def cap(self) -> float:
        return CAP_FACTOR * self.max_observed

    @property
    def unit_weight(self) -> float:
        """Dollars of monthly value per cent of total spend, per dollar of value per unit, at zero
        curvature: b for an additive metric, b / h for a team-level one."""
        return self.baseline if self.kind == ADDITIVE else self.baseline / self.headcount

    @property
    def weight(self) -> float:
        return self.value * self.unit_weight


@dataclass(frozen=True)
class Row:
    team_id: str
    amount_cents: int
    capped: bool
    funded_share_in_draws: float
    beyond_observed_share: float
    marginal_return: float | None  # value per $1 of spend at x0 plus the increment; funded teams only


@dataclass(frozen=True)
class Allocation:
    budget_cents: int
    rows: tuple[Row, ...]
    lam: float | None  # dollars of value per cent of spend at the solution

    @property
    def unallocated_cents(self) -> int:
        return self.budget_cents - sum(r.amount_cents for r in self.rows)


# ---- configuration -------------------------------------------------------------------------

@dataclass(frozen=True)
class Config:
    """The parts of `config/roi.yaml` the allocation and the agenda read:

        value_per_unit: {team_id: {low: .., base: .., high: ..}}   dollars per unit
        memo_budget_usd: monthly increment of the build
        rho: within-user correlation of the agenda (default 0.05)
        sigma_override: {team_id: sigma}   optional, in the metric's per-head units
    """

    value_per_unit: dict[str, tuple[float, float, float]]
    memo_budget_usd: float | None
    rho: float
    sigma_override: dict[str, float]

    def values(self, team: str) -> tuple[float, float, float]:
        try:
            return self.value_per_unit[team]
        except KeyError:
            raise RoiError(2, f"no value per unit configured for team {team}") from None


DEFAULT_RHO = 0.05


def config_path(work: WorkDir) -> Path:
    return work.root / "config" / "roi.yaml"


def load_config(path: Path) -> Config | None:
    """The config, or None when there is no file."""
    if not path.exists():
        return None
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    values = {}
    for team, v in (raw.get("value_per_unit") or {}).items():
        try:
            values[str(team)] = (float(v["low"]), float(v["base"]), float(v["high"]))
        except (KeyError, TypeError, ValueError):
            raise RoiError(2, f"{path}: value_per_unit for team {team} needs numeric low, base and high") from None
    budget = raw.get("memo_budget_usd")
    return Config(
        values,
        None if budget is None else float(budget),
        float(raw.get("rho", DEFAULT_RHO)),
        {str(t): float(s) for t, s in (raw.get("sigma_override") or {}).items()},
    )


# ---- solver ---------------------------------------------------------------------------------

def _key(text: str) -> int:
    return zlib.crc32(text.encode("utf-8"))


def value_draws(team_id: str, n: int = DRAWS) -> np.ndarray:
    """Uniform(0, 1) draws for one team's value per unit, from the child stream "value-draws" of
    the team, so they do not depend on the other teams or on their order."""
    seq = np.random.SeedSequence(VALUE_DRAW_SEED, spawn_key=(_key("value-draws"), _key(team_id)))
    return np.random.Generator(np.random.PCG64(seq)).uniform(0.0, 1.0, n)


def _spend(lam: np.ndarray, w: np.ndarray, a: np.ndarray, k: np.ndarray, x0: np.ndarray, cap: np.ndarray) -> np.ndarray:
    """Spend per head of each team at multiplier `lam` (D,) for weights `w` (D, n)."""
    with np.errstate(divide="ignore", invalid="ignore"):
        x = k * np.log(w * a / (k * lam[:, None]))
    x = np.where(np.isfinite(x) & (w > 0.0), x, x0)
    return np.clip(x, x0, cap)


def solve(w: np.ndarray, a: np.ndarray, k: np.ndarray, h: np.ndarray, x0: np.ndarray, cap: np.ndarray,
          budget_cents: float) -> tuple[np.ndarray, np.ndarray]:
    """Spend per head and the multiplier for each row of weights `w` (D, n).

    Where the budget is at least the sum of the headroom every team goes to its cap and the
    multiplier is NaN."""
    d, n = w.shape
    headroom = float((h * (cap - x0)).sum())
    x = np.tile(x0, (d, 1)).astype(float)
    lam = np.full(d, math.nan)
    if n == 0 or budget_cents <= 0.0:
        return x, lam
    if budget_cents >= headroom:
        return np.tile(np.where(w.min(axis=0) > 0.0, cap, x0), (d, 1)).astype(float), lam
    with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
        top = np.where(w > 0.0, w * a / k * np.exp(-x0 / k), 0.0)
        bottom = np.where(w > 0.0, w * a / k * np.exp(-cap / k), np.inf)
    hi = np.log(top.max(axis=1))
    lo = np.log(bottom.min(axis=1))
    for _ in range(BISECTIONS):
        mid = (lo + hi) / 2.0
        spent = (h * (_spend(np.exp(mid), w, a, k, x0, cap) - x0)).sum(axis=1)
        over = spent > budget_cents  # too much spent: raise lambda
        lo = np.where(over, mid, lo)
        hi = np.where(over, hi, mid)
    lam = np.exp((lo + hi) / 2.0)
    return _spend(lam, w, a, k, x0, cap), lam


def round_cents(real: list[float], eligible: list[bool], budget_cents: int) -> list[int]:
    """Largest-remainder rounding to cents. When the real amounts reach the budget they are
    rounded to sum to it exactly; otherwise (every cap binds first) they are floored."""
    floors = [math.floor(r) for r in real]
    if sum(real) < budget_cents - 1e-6:
        return floors
    left = budget_cents - sum(floors)
    order = sorted((i for i, ok in enumerate(eligible) if ok), key=lambda i: (-(real[i] - floors[i]), i))
    while left > 0 and order:
        for i in order:
            if left == 0:
                break
            floors[i] += 1
            left -= 1
    return floors


def allocate(teams: list[Team], budget_cents: int) -> Allocation:
    """Allocate `budget_cents` over `teams` (every measured team; only those with an identified
    curve can be funded). Rows are in team-id order."""
    teams = sorted(teams, key=lambda t: t.team_id)
    live = [t for t in teams if t.identified and t.weight > 0.0]
    n = len(live)
    arr = lambda f: np.array([f(t) for t in live], dtype=float)  # noqa: E731
    a, k, h = arr(lambda t: t.a), arr(lambda t: t.k), arr(lambda t: t.headcount)
    x0, cap = arr(lambda t: t.x0), arr(lambda t: t.cap)
    x, lam = solve(arr(lambda t: t.weight)[None, :], a, k, h, x0, cap, float(budget_cents))
    x, lam = x[0], float(lam[0])
    real = [float(v) for v in h * (x - x0)]
    at_cap = [bool(c >= cp - 1e-9 * cp) for c, cp in zip(x, cap)]
    cents = round_cents(real, [not c and r > 0.0 for c, r in zip(at_cap, real)], budget_cents)
    shares = funded_shares(live, budget_cents)

    position = {t.team_id: i for i, t in enumerate(live)}
    rows = []
    for t in teams:
        if t.team_id not in position:
            rows.append(Row(t.team_id, 0, False, 0.0, 0.0, None))
            continue
        i = position[t.team_id]
        step = x[i] - x0[i]
        beyond = 0.0 if step <= 0.0 else float(max(0.0, x[i] - t.max_observed) / step)
        funded = real[i] >= FUNDED_MIN_CENTS
        m = 100.0 * t.weight * curve.marginal(t.a, t.k, float(x[i])) if funded else None
        rows.append(Row(t.team_id, cents[i], bool(at_cap[i] and step > 0.0), shares[t.team_id], beyond, m))
    return Allocation(budget_cents, tuple(rows), None if math.isnan(lam) else lam)


def funded_shares(live: list[Team], budget_cents: int, draws: int = DRAWS) -> dict[str, float]:
    """Share of `draws` seeded draws of each team's value per unit (uniform between its low and
    high) in which the team is funded. Teams that cannot be funded are absent."""
    if not live:
        return {}
    u = np.column_stack([value_draws(t.team_id, draws) for t in live])
    low = np.array([t.value_low for t in live])
    high = np.array([t.value_high for t in live])
    unit = np.array([t.unit_weight for t in live])
    w = (low + u * (high - low)) * unit
    a = np.array([t.a for t in live], dtype=float)
    k = np.array([t.k for t in live], dtype=float)
    h = np.array([t.headcount for t in live], dtype=float)
    x0 = np.array([t.x0 for t in live])
    cap = np.array([t.cap for t in live])
    x, _ = solve(w, a, k, h, x0, cap, float(budget_cents))
    funded = (h * (x - x0)) >= FUNDED_MIN_CENTS
    return {t.team_id: float(funded[:, i].mean()) for i, t in enumerate(live)}


# ---- inputs from the warehouse ------------------------------------------------------------------

def teams_from_warehouse(con, through: str, cfg: Config) -> list[Team]:
    """Every measured team with its fitted curve (`fit_curve`), current spend and value per unit.
    Raises RoiError(2) naming a measured team that has no value per unit."""
    profile = curve.spend_profile(con, through)
    rows = con.execute(
        "SELECT e.team_id, d.kind, e.baseline_outcome, c.a, c.k, c.identified"
        " FROM est_team_effect e JOIN dim_team d ON d.team_id = e.team_id"
        " JOIN fit_curve c ON c.team_id = e.team_id AND c.through_month = e.through_month"
        " WHERE e.through_month = ? AND e.status = 'measured' ORDER BY e.team_id",
        [through],
    ).fetchall()
    out = []
    for team, kind, baseline, a, k, identified in rows:
        low, base, high = cfg.values(team)
        x0, max_observed, heads = profile[team]
        out.append(Team(team, heads, kind, float(baseline), a, k, bool(identified), x0, max_observed, base, low, high))
    return out


# ---- the fixture check ----------------------------------------------------------------------

def load_fixture(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except OSError:
        raise RoiError(2, f"cannot read the fixture {path}") from None


def fixture_teams(fixture: dict) -> list[Team]:
    out = []
    for r in fixture["teams"]:
        out.append(Team(
            r["team_id"], r["headcount"], r["kind"], r["baseline_outcome"], r["a"], r["k"], True,
            r["x0_cents"], r["max_observed_cents"], r["value_per_unit_usd"], r["value_per_unit_usd"],
            r["value_per_unit_usd"],
        ))
    return out


def check_fixture(fixture: dict, budget_usd: int) -> list[tuple[str, bool, str]]:
    """Compare the allocation of the fixture's curves with its hand-worked amounts.

    Returns (id, pass, detail) triples. Raises RoiError(2) when `budget_usd` is not the fixture's."""
    if budget_usd != fixture["budget_usd"]:
        raise RoiError(2, f"--budget {budget_usd} differs from the fixture's budget_usd {fixture['budget_usd']}")
    teams = fixture_teams(fixture)
    result = allocate(teams, budget_usd * 100)
    want = fixture["expected"]
    checks = []

    got = {r.team_id: r.amount_cents for r in result.rows}
    bad = [f"{t}: {got.get(t)} != {c}" for t, c in want["amount_cents"].items() if got.get(t) != c]
    checks.append(("amounts", not bad and set(got) == set(want["amount_cents"]),
                   "; ".join(bad) or "every team's amount equals the reference to the cent"))

    total = sum(got.values())
    ok = total + result.unallocated_cents == result.budget_cents and result.unallocated_cents == want["unallocated_cents"]
    checks.append(("sum", ok, f"amounts {total} + unallocated {result.unallocated_cents} = budget {result.budget_cents}"))

    capped = {r.team_id: r.capped for r in result.rows}
    bad = [f"{t}: {capped.get(t)} != {c}" for t, c in want["capped"].items() if capped.get(t) != c]
    checks.append(("capped", not bad, "; ".join(bad) or "the capped teams are those of the reference"))

    live = [r.marginal_return for r in result.rows if r.marginal_return is not None and not r.capped]
    spread = max(live) - min(live) if live else 0.0
    checks.append(("marginals", spread <= 1e-9,
                   f"{len(live)} funded, uncapped teams, marginal returns within {spread:.3e} of each other"))
    return checks


# ---- helpers of the read-only commands ----------------------------------------------------------

MONTH_PATTERN = r"\d{4}-(0[1-9]|1[0-2])"
FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures"


def open_readonly(work: WorkDir):
    import duckdb

    if not work.warehouse_db.exists():
        raise RoiError(2, f"no warehouse at {work.warehouse_db}; run `roi build` first")
    return duckdb.connect(str(work.warehouse_db), read_only=True)


def last_month(con) -> str:
    (last,) = con.execute("SELECT strftime(max(month), '%Y-%m') FROM fct_team_month").fetchone()
    if last is None:
        raise RoiError(2, "the warehouse has no panel rows")
    return last
