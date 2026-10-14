"""The generator on the fixture config: deterministic, consistent with its panel, tied to the ledger."""

import ast
import csv
import json
import math
import statistics
from collections import defaultdict
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from roi.cli import main
from roi.gen import config, panel

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "gen_fixture.yaml"
REAL_CONFIG = ROOT / "config" / "gen.yaml"

# Rollout month of each team in the Beta Co design; None is the control (never treated).
ROLLOUTS = {
    "platform_engineering": "2025-04",
    "support": "2025-07",
    "sales_development": "2025-08",
    "product_engineering": "2025-09",
    "marketing": "2025-11",
    "finance_operations": "2025-12",
    "legal_operations": "2026-02",
    "people_operations": None,
}
MEASURED_THROUGH_MARCH = ("support", "sales_development", "product_engineering", "marketing", "finance_operations")


def gen(work, *args, config_path=FIXTURE):
    return CliRunner().invoke(main, ["gen", "--config", str(config_path), "--work-dir", str(work), *args])


def read(path):
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def tree(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


@pytest.fixture(scope="module")
def cfg():
    return config.load(FIXTURE)


@pytest.fixture(scope="module")
def sim(cfg):
    return panel.simulate(cfg)


@pytest.fixture(scope="module")
def work(tmp_path_factory):
    work = tmp_path_factory.mktemp("gen")
    result = gen(work)
    assert result.exit_code == 0, result.output
    return work


def test_two_runs_are_byte_identical(work, tmp_path):
    assert gen(tmp_path).exit_code == 0
    first, second = tree(work), tree(tmp_path)
    assert second == first
    assert "truth/truth.json" in first and "data/books/raw_ai_spend.csv" in first


def test_seed_changes_the_data(cfg, sim):
    other = panel.simulate(cfg, seed=cfg.seed + 1)
    assert not all((a.output == b.output).all() for a, b in zip(sim.teams, other.teams))


def test_rollout_months_match_the_design(work, cfg):
    first_spend = {}
    for row in read(work / "data" / "books" / "raw_ai_spend.csv"):
        if int(row["cost_cents"]) > 0 and row["team_id"] != "unassigned":
            month = row["usage_date"][:7]
            first_spend[row["team_id"]] = min(month, first_spend.get(row["team_id"], month))
    assert first_spend == {t: m for t, m in ROLLOUTS.items() if m is not None}
    truth = json.loads((work / "truth" / "truth.json").read_text())
    assert {t: v["rollout_month"] for t, v in truth["teams"].items()} == ROLLOUTS
    assert [t.team_id for t in cfg.teams] == list(ROLLOUTS)


def test_books_sum_to_the_panel(work, sim):
    spend = defaultdict(int)
    for row in read(work / "data" / "books" / "raw_ai_spend.csv"):
        spend[(row["team_id"], row["usage_date"][:7])] += int(row["cost_cents"])
    outputs = {(r["team_id"], r["month"][:7]): float(r["value"])
               for r in read(work / "data" / "books" / "raw_output.csv")}
    books_months = [m for m in sim.months if m <= "2026-03"]
    for tp in sim.teams:
        for i, month in enumerate(sim.months):
            if month in books_months:
                assert spend.get((tp.spec.team_id, month), 0) == tp.spend_cents[i]
                assert outputs[(tp.spec.team_id, month)] == pytest.approx(tp.output[i], abs=1e-6)
            else:
                assert (tp.spec.team_id, month) not in outputs
    for i, month in enumerate(sim.months):
        if month in books_months:
            assert spend.get(("unassigned", month), 0) == sim.unassigned_cents[i] > 0


def test_gl_equals_spend_for_every_team_month_including_unassigned(work, cfg):
    spend = defaultdict(int)
    for row in read(work / "data" / "books" / "raw_ai_spend.csv"):
        spend[(row["team_id"], row["usage_date"][:7])] += int(row["cost_cents"])
    gl = defaultdict(int)
    other_accounts = 0
    for row in read(work / "data" / "books" / "raw_gl_journal.csv"):
        if row["account_code"] == cfg.gl_account:
            gl[(row["cost_center"], row["period"])] += int(row["debit_cents"]) - int(row["credit_cents"])
        else:
            other_accounts += 1
    assert other_accounts > 0
    assert gl == {k: v for k, v in spend.items() if v}
    assert any(team == "unassigned" for team, _ in gl)


def test_ids_and_roster_are_consistent(work, cfg):
    books = work / "data" / "books"
    teams = {r["team_id"]: r for r in read(books / "dim_team.csv")}
    assert set(teams) == set(ROLLOUTS)
    roster = read(books / "raw_hr_roster.csv")
    employees = {r["employee_id"] for r in roster}
    assert len(employees) == len(roster) == sum(t.headcount for t in cfg.teams)
    spend_users = {r["user_id"] for r in read(books / "raw_ai_spend.csv") if r["team_id"] != "unassigned"}
    assert spend_users <= employees
    grain = [(r["usage_date"], r["user_id"], r["project_id"], r["provider"], r["model"])
             for r in read(books / "raw_ai_spend.csv")]
    assert len(set(grain)) == len(grain)
    assert teams["finance_operations"]["direction"] == "-1" and teams["finance_operations"]["kind"] == "team_level"


def test_spend_ramps_to_the_target(sim, cfg):
    for tp in sim.teams:
        spec = tp.spec
        if spec.rollout_month is None:
            assert not tp.spend_cents.any()
            continue
        start = sim.months.index(spec.rollout_month)
        target = spec.headcount * spec.target_spend_per_head_usd * 100
        for j in range(start, len(sim.months)):
            ramp = min(1.0, (j - start + 1) / cfg.ramp_months)
            assert tp.spend_cents[j] == round(target * ramp)
        assert not tp.spend_cents[:start].any()


def test_truth_holds_the_planted_effects(work, cfg):
    truth = json.loads((work / "truth" / "truth.json").read_text())
    assert truth["seed"] == cfg.seed
    assert truth["teams"]["people_operations"]["planted_effect_by_month"] == {}
    assert truth["teams"]["people_operations"]["curve"] is None
    support = truth["teams"]["support"]
    spec = next(t for t in cfg.teams if t.team_id == "support")
    effects = support["planted_effect_by_month"]
    assert list(effects)[0] == "2025-07" and list(effects)[-1] == "2026-04"
    # At the target spend a planted effect equals ln(1 + figure) and the curve is at 63% of its asymptote.
    assert effects["2025-09"] == pytest.approx(math.log1p(spec.effect_at_target))
    assert effects["2026-04"] == pytest.approx(effects["2025-09"])
    assert effects["2025-07"] < effects["2025-08"] < effects["2025-09"]
    assert support["curve"]["k"] == spec.target_spend_per_head_usd
    assert support["curve"]["A"] * (1 - math.exp(-1)) == pytest.approx(math.log1p(spec.effect_at_target))
    # Effects are signed by direction: a lower-is-better team still has a positive (improving) effect.
    assert all(v > 0 for v in truth["teams"]["finance_operations"]["planted_effect_by_month"].values())


def test_effects_move_the_outcome_in_the_right_direction(sim):
    finance = sim.team("finance_operations")
    treated = sim.months.index("2026-02")
    assert finance.outcome[treated:].mean() < finance.outcome[:treated].mean()
    support = sim.team("support")
    treated = sim.months.index("2025-09")
    assert support.outcome[treated:].mean() > support.outcome[:treated].mean()


def test_noise_of_unmeasured_teams_is_the_median_of_the_measured(cfg):
    sds = {t.team_id: t.noise_sd for t in cfg.teams}
    median = statistics.median(sds[t] for t in MEASURED_THROUGH_MARCH)
    assert sds["platform_engineering"] == sds["people_operations"] == median


def test_real_config_with_the_empty_table_exits_4():
    assert yaml.safe_load(REAL_CONFIG.read_text())["teams"] == []
    result = CliRunner().invoke(main, ["gen", "--config", str(REAL_CONFIG)])
    assert result.exit_code == 4
    assert "fill the team table in config/gen.yaml" in result.output


def test_an_incomplete_row_exits_4_asking_for_the_table(tmp_path):
    raw = yaml.safe_load(FIXTURE.read_text())
    del raw["teams"][0]["noise_sd"]
    path = tmp_path / "gen.yaml"
    path.write_text(yaml.safe_dump(raw))
    result = gen(tmp_path / "w", config_path=path)
    assert result.exit_code == 4
    assert "noise_sd" in result.output and "config/gen.yaml" in result.output
    assert not (tmp_path / "w").exists()


def test_a_missing_config_exits_2(tmp_path):
    assert gen(tmp_path, config_path=tmp_path / "absent.yaml").exit_code == 2


def test_the_generator_never_imports_the_truth_modules():
    for path in sorted((ROOT / "src" / "roi" / "gen").glob("*.py")) + [ROOT / "src" / "roi" / "commands" / "gen.py"]:
        for node in ast.walk(ast.parse(path.read_text())):
            names = []
            if isinstance(node, ast.ImportFrom):
                names = [node.module or ""] + [f"{node.module}.{a.name}" for a in node.names]
            elif isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            assert not any(n.startswith(("roi.truth", "roi.verify_truth")) for n in names), path.name
