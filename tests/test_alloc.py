"""Allocation: the hand-worked fixture, an independent closed-form reference, invariants on random
concave curves, the value draws, and the curve/alloc/agenda build steps on a synthetic company."""

import json
import math
import os
import shutil
from pathlib import Path

import duckdb
import numpy as np
import pytest
from click.testing import CliRunner

import roi.commands.allocate as allocate_command
from roi import alloc, curve
from roi.cli import main
from roi.steps import BuildContext, agenda as agenda_step, alloc as alloc_step, curve as curve_step, estimate
from roi.workdir import RoiError, WorkDir

FIXTURE = Path(__file__).parent / "fixtures" / "alloc_small.json"


def team(team_id="t", h=100, kind="additive", b=10.0, a=0.2, k=10000.0, x0=10000.0, mx=None, v=50.0,
         lo=None, hi=None, identified=True):
    return alloc.Team(team_id, h, kind, b, a, k, identified, x0, x0 if mx is None else mx, v,
                      v if lo is None else lo, v if hi is None else hi)


def reference(teams, budget_cents):
    """Closed form by active set, independent of the bisection: with the capped teams fixed,
    ln(lambda) = (sum h k ln c - sum h x0 - budget) / sum h k over the funded, uncapped teams."""
    c = {t.team_id: t.weight * t.a / t.k for t in teams}
    capped: set[str] = set()
    while True:
        rest = budget_cents - sum(t.headcount * (t.cap - t.x0) for t in teams if t.team_id in capped)
        funded = [t for t in teams if t.team_id not in capped]
        while True:
            if not funded:
                return {t.team_id: (t.headcount * (t.cap - t.x0) if t.team_id in capped else 0.0) for t in teams}
            ln_lam = (sum(t.headcount * t.k * math.log(c[t.team_id]) for t in funded)
                      - sum(t.headcount * t.x0 for t in funded) - rest) / sum(t.headcount * t.k for t in funded)
            drop = [t for t in funded if math.log(c[t.team_id]) - t.x0 / t.k <= ln_lam]
            if not drop:
                break
            funded = [t for t in funded if t not in drop]
        x = {t.team_id: t.k * (math.log(c[t.team_id]) - ln_lam) for t in funded}
        over = {t.team_id for t in funded if x[t.team_id] > t.cap}
        if not over:
            return {t.team_id: (t.headcount * (t.cap - t.x0) if t.team_id in capped
                                else t.headcount * (x[t.team_id] - t.x0) if t.team_id in x else 0.0) for t in teams}
        capped |= over


# ---- the fixture ----------------------------------------------------------------------------

def test_the_fixture_matches_its_hand_worked_amounts():
    fixture = alloc.load_fixture(FIXTURE)
    checks = alloc.check_fixture(fixture, 50000)
    assert [c[0] for c in checks] == ["amounts", "sum", "capped", "marginals"]
    assert all(ok for _, ok, _ in checks), checks
    assert fixture["budget_usd"] == 50000


def test_the_fixture_equals_the_closed_form_reference():
    fixture = alloc.load_fixture(FIXTURE)
    teams = alloc.fixture_teams(fixture)
    real = reference(teams, 5_000_000)
    got = {r.team_id: r.amount_cents for r in alloc.allocate(teams, 5_000_000).rows}
    for t, want in fixture["expected"]["amount_cents"].items():
        assert abs(real[t] - want) < 1.0, (t, real[t])
        assert got[t] == want
    assert sum(got.values()) == 5_000_000


def test_the_fixture_hand_numbers():
    teams = {t.team_id: t for t in alloc.fixture_teams(alloc.load_fixture(FIXTURE))}
    assert [teams[t].weight for t in ("finance", "sales", "support")] == [2700.0, 875.0, 600.0]
    assert teams["finance"].cap == 15000.0
    lam = alloc.allocate(list(teams.values()), 5_000_000).lam
    assert lam == pytest.approx(0.00206854, rel=1e-6)
    assert math.log(lam) == pytest.approx(-6.180912, abs=1e-6)


def test_check_exits_2_on_a_different_budget_and_1_on_a_different_amount(tmp_path, monkeypatch):
    runner = CliRunner()
    ok = runner.invoke(main, ["allocate", "--budget", "50000", "--check"])
    assert ok.exit_code == 0 and "allocation: matches reference" in ok.output
    other = runner.invoke(main, ["allocate", "--budget", "40000", "--check"])
    assert other.exit_code == 2 and "differs from the fixture's budget_usd" in other.output

    fixture = json.loads(FIXTURE.read_text())
    fixture["expected"]["amount_cents"]["sales"] += 1
    fixture["expected"]["amount_cents"]["support"] -= 1
    bad = tmp_path / "alloc_bad.json"
    bad.write_text(json.dumps(fixture))
    monkeypatch.setattr(allocate_command, "FIXTURE", bad)
    wrong = runner.invoke(main, ["allocate", "--budget", "50000", "--check"])
    assert wrong.exit_code == 1 and "FAIL amounts" in wrong.output


def test_check_json_report():
    out = CliRunner().invoke(main, ["allocate", "--budget", "50000", "--check", "--json"])
    report = json.loads(out.output)
    assert report["command"] == "allocate" and report["status"] == "pass"
    assert {c["id"] for c in report["checks"]} == {"amounts", "sum", "capped", "marginals"}


# ---- rounding ---------------------------------------------------------------------------------

def test_largest_remainder_rounding():
    assert alloc.round_cents([1.4, 2.4, 3.2], [True] * 3, 7) == [2, 2, 3]  # tie of fractions: lower index first
    assert alloc.round_cents([1.9, 2.5, 2.6], [True] * 3, 7) == [2, 2, 3]
    assert alloc.round_cents([1.0, 2.7, 3.3], [False, True, True], 7) == [1, 3, 3]  # a capped team is never topped up
    assert alloc.round_cents([1.5, 2.5], [True, True], 10) == [1, 2]  # the caps bind: floors, 7 unallocated


# ---- invariants on random concave curves --------------------------------------------------------

def random_teams(rng):
    out = []
    for i in range(int(rng.integers(1, 7))):
        k = float(rng.uniform(2000, 40000))
        x0 = float(rng.uniform(0, 2 * k))
        out.append(team(
            f"t{i}", int(rng.integers(5, 400)), str(rng.choice(["additive", "team_level"])), float(rng.uniform(1, 50)),
            float(rng.uniform(0.02, 0.4)), k, x0, x0 * float(rng.uniform(1.0, 1.3)) or 1.0, float(rng.uniform(5, 500)),
        ))
    return out


@pytest.mark.parametrize("seed", range(40))
def test_allocation_invariants_on_random_concave_curves(seed):
    rng = np.random.default_rng(seed)
    teams = random_teams(rng)
    headroom = sum(t.headcount * (t.cap - t.x0) for t in teams)
    budget = int(rng.uniform(0.0, 1.3) * headroom)
    result = alloc.allocate(teams, budget)
    rows = {r.team_id: r for r in result.rows}
    by = {t.team_id: t for t in teams}

    assert all(r.amount_cents >= 0 for r in result.rows)  # no cuts
    assert result.unallocated_cents >= 0
    assert sum(r.amount_cents for r in result.rows) + result.unallocated_cents == budget
    if result.unallocated_cents:
        assert all(r.capped for r in result.rows if by[r.team_id].weight > 0)  # only when every cap binds first
    for r in result.rows:
        assert r.amount_cents <= by[r.team_id].headcount * (by[r.team_id].cap - by[r.team_id].x0) + 1.0

    uncapped = [r.marginal_return for r in result.rows if r.marginal_return is not None and not r.capped]
    if uncapped:
        assert max(uncapped) - min(uncapped) <= 1e-9 * max(1.0, max(uncapped))
        lam = 100.0 * result.lam
        assert uncapped[0] == pytest.approx(lam, rel=1e-9)
    if result.lam is not None:
        for r in result.rows:  # a team that gets nothing is one whose marginal return at x0 is below lambda
            t = by[r.team_id]
            at_x0 = 100.0 * t.weight * curve.marginal(t.a, t.k, t.x0)
            if r.amount_cents == 0 and not r.capped:
                assert at_x0 <= 100.0 * result.lam * (1 + 1e-6)
            if r.capped and r.amount_cents > 0:
                assert 100.0 * t.weight * curve.marginal(t.a, t.k, t.cap) >= 100.0 * result.lam * (1 - 1e-9)

    # A capped team keeps its floor; the cents its fraction leaves go to uncapped teams.
    slack = 1.0 + sum(r.capped for r in result.rows) + 1e-6
    for t, amount in reference(teams, budget).items():
        assert abs(rows[t].amount_cents - amount) <= slack, (t, rows[t].amount_cents, amount)


def test_a_team_below_the_multiplier_gets_nothing():
    teams = [team("high", v=200.0), team("low", v=2.0)]
    result = {r.team_id: r for r in alloc.allocate(teams, 100_000).rows}
    assert result["low"].amount_cents == 0 and not result["low"].capped
    assert result["high"].amount_cents == 100_000
    assert result["low"].marginal_return is None


def test_budget_beyond_the_caps_reports_the_remainder_as_unallocated():
    teams = [team("a", h=10), team("b", h=20, k=5000.0, x0=5000.0)]
    headroom = [t.headcount * (t.cap - t.x0) for t in teams]  # 10 x 5,000 and 20 x 2,500
    result = alloc.allocate(teams, 1_000_000)
    assert [r.amount_cents for r in result.rows] == [int(h) for h in headroom]
    assert all(r.capped for r in result.rows)
    assert result.unallocated_cents == 1_000_000 - 100_000


def test_zero_budget_and_unidentified_curves():
    teams = [team("a"), team("b", identified=False), team("c", a=-0.1, identified=True)]
    assert [r.amount_cents for r in alloc.allocate(teams, 0).rows] == [0, 0, 0]
    result = alloc.allocate([team("a"), team("b", identified=False)], 50_000)
    assert {r.team_id: r.amount_cents for r in result.rows} == {"a": 50_000, "b": 0}
    assert {r.team_id: r.funded_share_in_draws for r in result.rows}["b"] == 0.0


def test_no_team_can_be_funded():
    result = alloc.allocate([team("a", identified=False)], 50_000)
    assert result.unallocated_cents == 50_000 and result.lam is None
    assert alloc.allocate([], 100).unallocated_cents == 100


def test_beyond_observed_share_is_the_part_of_the_increment_above_the_highest_spend():
    t = team("a", h=10, x0=10000.0, mx=12000.0)  # cap 18,000; the increment is 100,000 cents = 10,000 per head
    row = alloc.allocate([t], 100_000).rows[0]
    assert row.capped  # 10,000 + 10,000 is above the cap of 18,000, so x = 18,000
    assert row.beyond_observed_share == pytest.approx(0.75)  # 6,000 of the 8,000 increment is above 12,000
    row = alloc.allocate([t], 50_000).rows[0]  # x = 15,000: 3,000 of the 5,000 increment is above 12,000
    assert row.beyond_observed_share == pytest.approx(0.6)
    flat = team("b", h=10, x0=10000.0, mx=10000.0)
    assert alloc.allocate([flat], 20_000).rows[0].beyond_observed_share == pytest.approx(1.0)


# ---- the value draws ----------------------------------------------------------------------------

def test_value_draws_are_seeded_and_independent_of_the_other_teams():
    a = alloc.value_draws("alpha")
    assert a.shape == (alloc.DRAWS,) and 0.0 <= a.min() and a.max() < 1.0
    assert np.array_equal(a, alloc.value_draws("alpha"))
    assert not np.array_equal(a, alloc.value_draws("beta"))
    first = alloc.funded_shares([team("alpha", lo=10, hi=90), team("beta", lo=10, hi=90)], 50_000)
    swapped = alloc.funded_shares([team("beta", lo=10, hi=90), team("alpha", lo=10, hi=90)], 50_000)
    assert first == swapped


def test_funded_share_counts_the_draws_in_which_a_team_is_funded():
    # A tiny budget goes only to the team with the higher marginal return at x0. Team x has a fixed
    # value; team y is funded exactly in the draws where its value per unit beats x's marginal.
    x = team("x", v=100.0)
    y = team("y", v=50.0, lo=20.0, hi=180.0)
    budget = 100  # one dollar
    threshold = x.weight * curve.marginal(x.a, x.k, x.x0) / (y.unit_weight * curve.marginal(y.a, y.k, y.x0))
    u = alloc.value_draws("y")
    values = 20.0 + u * (180.0 - 20.0)
    expected = float(np.mean(values > threshold))
    assert 0.0 < expected < 1.0
    shares = alloc.funded_shares([x, y], budget)
    assert shares["y"] == pytest.approx(expected, abs=0.002)  # draws within a hair of the threshold may differ


def test_a_team_that_wins_at_its_lowest_value_is_always_funded():
    shares = alloc.funded_shares([team("x", v=500.0, lo=400.0, hi=600.0), team("y", v=1.0, lo=0.5, hi=1.5)], 10_000)
    assert shares == {"x": 1.0, "y": 0.0}


# ---- the build steps on a synthetic company --------------------------------------------------------

MONTHS = [f"2025-{m:02d}" for m in range(1, 13)]
# team: headcount, baseline, rollout index (None: never), A, k, target spend, kind, direction
COMPANY = {
    "ctrl1": (50, 20.0, None, 0.0, 1.0, 0.0, "additive", 1),
    "ctrl2": (50, 15.0, None, 0.0, 1.0, 0.0, "additive", 1),
    "early": (60, 10.0, 3, 0.20, 10000.0, 10000.0, "additive", 1),
    "mid": (80, 25.0, 5, 0.30, 20000.0, 20000.0, "additive", 1),
    "flat": (40, 12.0, 4, 0.0, 10000.0, 10000.0, "additive", 1),
    "fresh": (30, 8.0, 1, 0.20, 10000.0, 10000.0, "additive", 1),
    "recent": (30, 9.0, 10, 0.20, 10000.0, 10000.0, "additive", 1),
}
VALUES = """
memo_budget_usd: 5000
rho: 0.05
value_per_unit:
{rows}
"""


def config_text(skip=()):
    rows = "\n".join(
        f"  {t}: {{low: {v * 0.5}, base: {v}, high: {v * 1.5}}}"
        for t, v in {"early": 40, "mid": 20, "flat": 30, "fresh": 25, "recent": 25, "ctrl1": 10, "ctrl2": 10}.items()
        if t not in skip
    )
    return VALUES.format(rows=rows)


def build_company(db: Path, noise=0.003, seed=7):
    rng = np.random.default_rng(seed)
    shock = rng.normal(0.0, 0.03, len(MONTHS))
    con = duckdb.connect(str(db))
    con.execute(
        "CREATE TABLE fct_team_month(team_id VARCHAR, month DATE, outcome DOUBLE, y_log DOUBLE, spend_cents BIGINT,"
        " spend_per_head_cents DOUBLE, headcount INTEGER, rollout_month DATE)"
    )
    con.execute(
        "CREATE TABLE dim_team(team_id VARCHAR, name VARCHAR, metric_id VARCHAR, unit_label VARCHAR,"
        " direction TINYINT, kind VARCHAR)"
    )
    rows = []
    for tid, (h, b, r, a, k, target, kind, d) in COMPANY.items():
        con.execute("INSERT INTO dim_team VALUES (?, ?, 'm', 'u', ?, ?)", [tid, tid, d, kind])
        for i, m in enumerate(MONTHS):
            x = 0.0 if r is None or i < r else target * min(1.0, (i - r + 1) / 3)
            effect = a * (1 - math.exp(-x / k)) if r is not None else 0.0
            y = math.log(b) + shock[i] + rng.normal(0.0, noise) + effect
            rows.append((tid, f"{m}-01", math.exp(y), y, int(round(x * h)), x, h, None if r is None else f"{MONTHS[r]}-01"))
    con.executemany("INSERT INTO fct_team_month VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
    con.close()


def run_steps(work: WorkDir, steps=(estimate, curve_step, alloc_step, agenda_step)) -> Path:
    work.warehouse.mkdir(parents=True, exist_ok=True)
    build_company(work.warehouse_next_db)
    ctx = BuildContext(work, work.warehouse_next_db, MONTHS[-1])
    for step in steps:
        step.run(ctx)
    os.replace(work.warehouse_next_db, work.warehouse_db)
    return work.warehouse_db


@pytest.fixture(scope="module")
def company(tmp_path_factory):
    work = WorkDir(tmp_path_factory.mktemp("company"))
    (work.root / "config").mkdir()
    (work.root / "config" / "roi.yaml").write_text(config_text())
    run_steps(work)
    return work


def rows_of(work, sql):
    con = duckdb.connect(str(work.warehouse_db), read_only=True)
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


def test_the_curve_step_fits_the_measured_teams_only(company):
    fits = {r[0]: r for r in rows_of(company, "SELECT team_id, a, k, identified FROM fit_curve")}
    assert set(fits) == {"early", "flat", "mid"}  # no row for the controls or the unmeasured teams
    assert fits["early"][3] and fits["mid"][3] and not fits["flat"][3]
    assert fits["early"][1] == pytest.approx(0.20, rel=0.2) and fits["early"][2] == pytest.approx(10000.0, rel=0.2)
    assert fits["mid"][1] == pytest.approx(0.30, rel=0.2) and fits["mid"][2] == pytest.approx(20000.0, rel=0.2)


def test_the_alloc_step_funds_identified_teams_and_sums_to_the_budget(company):
    out = {r[0]: r for r in rows_of(
        company, "SELECT team_id, amount_cents, capped, funded_share_in_draws, beyond_observed_share, budget_cents,"
                 " through_month FROM alloc_result")}
    assert set(out) == {"early", "flat", "mid"}
    assert sum(r[1] for r in out.values()) == 500_000 and {r[5] for r in out.values()} == {500_000}
    assert out["flat"][1] == 0 and out["flat"][3] == 0.0
    assert out["early"][1] > 0 and out["mid"][1] > 0
    assert 0.0 < out["early"][3] <= 1.0 and 0.0 < out["mid"][3] <= 1.0
    assert out["early"][4] == pytest.approx(1.0) and out["mid"][4] == pytest.approx(1.0)
    assert {r[6] for r in out.values()} == {MONTHS[-1]}


def test_the_agenda_step_has_an_item_for_each_unmeasured_team_and_no_other(company):
    items = {r[0]: r for r in rows_of(
        company, "SELECT team_id, design, n_per_arm, length_months, rho, alpha, power, baseline_basis FROM agenda_item")}
    assert set(items) == {"fresh", "recent"}
    assert items["fresh"][1] in ("holdback", "not_testable_alone") and items["fresh"][7] == "pre"  # one pre month
    assert items["recent"][1] in ("wait", "not_testable_alone") and items["recent"][7] == "pre"
    assert all(r[4:7] == (0.05, 0.05, 0.8) for r in items.values())


def test_a_measured_team_without_a_value_per_unit_stops_the_alloc_step(tmp_path):
    work = WorkDir(tmp_path)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "roi.yaml").write_text(config_text(skip=("mid",)))
    with pytest.raises(RoiError) as e:
        run_steps(work)
    assert e.value.exit_code == 2 and "mid" in e.value.message


def test_without_a_config_the_alloc_and_agenda_tables_are_written_empty(tmp_path):
    work = WorkDir(tmp_path)
    run_steps(work)
    assert rows_of(work, "SELECT count(*) FROM alloc_result") == [(0,)]
    assert rows_of(work, "SELECT count(*) FROM agenda_item") == [(0,)]
    assert rows_of(work, "SELECT count(*) FROM fit_curve")[0][0] == 3


def test_the_steps_are_deterministic(company, tmp_path):
    other = WorkDir(tmp_path)
    (tmp_path / "config").mkdir()
    shutil.copy(company.root / "config" / "roi.yaml", tmp_path / "config" / "roi.yaml")
    run_steps(other)
    for table in ("fit_curve", "alloc_result", "agenda_item"):
        assert rows_of(company, f"SELECT * FROM {table} ORDER BY ALL") == rows_of(other, f"SELECT * FROM {table} ORDER BY ALL")


def test_allocate_command_prints_the_allocation(company):
    out = CliRunner().invoke(main, ["--work-dir", str(company.root), "allocate", "--budget", "5000"])
    assert out.exit_code == 0, out.output
    lines = {line.split()[0]: line for line in out.output.splitlines() if line.split() and line.split()[0] in ("early", "mid", "flat")}
    assert set(lines) == {"early", "mid", "flat"}
    assert "unallocated: $0.00" in out.output
    stored = {r[0]: r[1] for r in rows_of(company, "SELECT team_id, amount_cents FROM alloc_result")}
    result = json.loads(CliRunner().invoke(
        main, ["--work-dir", str(company.root), "allocate", "--budget", "5000", "--json"]).output)
    assert {t: v["amount_cents"] for t, v in result["result"]["teams"].items()} == stored


def test_allocate_command_errors(company, tmp_path):
    runner = CliRunner()
    assert runner.invoke(main, ["--work-dir", str(tmp_path), "allocate", "--budget", "100"]).exit_code == 2  # no config
    (tmp_path / "config").mkdir()
    shutil.copy(company.root / "config" / "roi.yaml", tmp_path / "config" / "roi.yaml")
    out = runner.invoke(main, ["--work-dir", str(tmp_path), "allocate", "--budget", "100"])
    assert out.exit_code == 2 and "run `roi build` first" in out.output
    bad = runner.invoke(main, ["--work-dir", str(company.root), "allocate", "--budget", "100", "--through", "2025-13"])
    assert bad.exit_code == 2
    assert runner.invoke(main, ["allocate"]).exit_code == 2  # --budget is required
