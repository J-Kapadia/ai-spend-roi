"""The testing agenda: the formulas against independent loops over the hand-worked fixture rows."""

import json
import math
import statistics
from dataclasses import replace
from pathlib import Path
from statistics import NormalDist

import pytest
from click.testing import CliRunner
from scipy import stats

from conftest import connect
import roi.commands.agenda as agenda_command
from roi import agenda, alloc, estimator
from roi.cli import main
from roi.workdir import RoiError

FIXTURE = Path(__file__).parent / "fixtures" / "agenda_small.json"
ROWS = json.loads(FIXTURE.read_text())["rows"]
Z = NormalDist().inv_cdf(0.975) + NormalDist().inv_cdf(0.80)


def row(ident):
    return next(r for r in ROWS if r["id"] == ident)


def reference(inputs):
    """The design, evaluated with plain loops from the formulas, not from roi.agenda."""
    i = dict(inputs)
    pre, post = i["pre_levels"], i["post_levels"]
    levels = pre if len(pre) >= 2 else pre + post
    if "sigma_override" in i:
        sigma = i["sigma_override"]
    elif i["kind"] == "team_level":
        sigma = statistics.stdev(levels)
    else:
        sigma = statistics.stdev([math.log(v) for v in levels]) * i["baseline"] * math.sqrt(i["team_size"])
    spend_dollars = i["x0_cents"] / 100.0 * (i["team_size"] if i["kind"] == "team_level" else 1)
    delta = spend_dollars / i["value_per_unit"]
    out = {"sigma": sigma, "delta": delta, "n_per_arm": None, "length_months": None}

    if i["reason"] == "too_recent":
        sd = math.sqrt(sum(i["cell_variances"]) / len(i["cell_variances"]))
        t = stats.t.ppf(0.975, i["df"])
        target = math.log(1 + delta / i["baseline"]) if i["direction"] > 0 else -math.log(1 - delta / i["baseline"])
        out.update(sigma=sd, delta=target, design="not_testable_alone")
        for m in range(1, 13):
            n = i["n_post"] + m
            if t * sd * math.sqrt((n + 1) / (2 * n)) <= target:
                out.update(design="wait", length_months=m)
                break
        return out

    size = i["team_size"] + i.get("pair_size", 0)
    out["design"] = "not_testable_alone"
    for length in range(1, 7):
        n = 2 * Z**2 * sigma**2 * (1 + (length - 1) * i["rho"]) / (length * delta**2)
        if math.ceil(n - 1e-9) <= size / 2:
            out.update(design="pair_stagger" if i["reason"] == "no_comparison" else "holdback",
                       n_per_arm=math.ceil(n - 1e-9), length_months=length)
            break
    return out


@pytest.mark.parametrize("fixture_row", ROWS, ids=[r["id"] for r in ROWS])
def test_every_fixture_row_equals_the_reference_and_the_hand_worked_values(fixture_row):
    ref = reference(fixture_row["inputs"])
    want = fixture_row["expected"]
    for field in ("design", "n_per_arm", "length_months"):
        assert ref[field] == want[field], field
    assert ref["sigma"] == pytest.approx(want["sigma"], rel=1e-12)
    assert ref["delta"] == pytest.approx(want["delta"], rel=1e-12)

    got = agenda.design(agenda.fixture_inputs(fixture_row))
    assert (got.design, got.n_per_arm, got.length_months) == (want["design"], want["n_per_arm"], want["length_months"])
    assert got.sigma == pytest.approx(want["sigma"], rel=1e-9)
    assert got.delta == pytest.approx(want["delta"], rel=1e-9)
    assert (got.alpha, got.power, got.rho) == (0.05, 0.80, 0.05)


def test_the_fixture_covers_every_reason_branch_and_both_sides_of_the_floor():
    reasons = [r["inputs"]["reason"] for r in ROWS]
    assert set(reasons) == set(estimator.REASONS)
    assert reasons.count("too_recent") >= 2
    wait = [r["expected"]["design"] for r in ROWS if r["inputs"]["reason"] == "too_recent"]
    assert "wait" in wait and "not_testable_alone" in wait
    # the sigma branches: pre-rollout months, all months, a team-level metric, a configured override
    assert any(r["inputs"]["baseline_basis"] == "pre" and len(r["inputs"]["pre_levels"]) >= 2 for r in ROWS)
    assert any(r["inputs"]["baseline_basis"] == "all" and not r["inputs"]["pre_levels"] for r in ROWS)
    assert any(r["inputs"]["kind"] == "team_level" for r in ROWS)
    assert any("sigma_override" in r["inputs"] for r in ROWS)
    assert any(r["inputs"]["direction"] < 0 for r in ROWS)


def test_every_reason_maps_to_exactly_one_design():
    assert set(agenda.DESIGN_BY_REASON) == set(estimator.REASONS)
    assert agenda.DESIGN_BY_REASON == {
        "no_pre_period": "holdback", "pre_trend": "holdback", "variance_not_computable": "holdback",
        "too_recent": "wait", "no_comparison": "pair_stagger",
    }
    for r in ROWS:
        got = agenda.design(agenda.fixture_inputs(r))
        assert got.design in (agenda.DESIGN_BY_REASON[r["inputs"]["reason"]], agenda.NOT_TESTABLE)
    with pytest.raises(RoiError) as e:
        agenda.design(replace(agenda.fixture_inputs(ROWS[0]), reason="not_a_reason"))
    assert e.value.exit_code == 2


def test_sample_size_formula():
    assert Z == pytest.approx(2.801585, abs=1e-6)
    assert agenda.Z == pytest.approx(Z, abs=1e-12)
    # sigma 10, delta 2, one month, rho irrelevant: 2 Z^2 x 25 = 392.44
    assert agenda.sample_size(10.0, 2.0, 0.05, 1) == pytest.approx(2 * Z**2 * 25, rel=1e-12)
    assert agenda.sample_size(10.0, 2.0, 0.05, 1) == pytest.approx(392.444, abs=1e-3)
    # two months: x (1 + rho) / 2
    assert agenda.sample_size(10.0, 2.0, 0.05, 2) == pytest.approx(392.444 * 1.05 / 2, abs=1e-3)
    assert agenda.holdback(10.0, 2.0, 0.05, 100.0) is None  # 392 does not fit; even 6 months needs 98 > 50
    assert agenda.holdback(10.0, 2.0, 0.05, 400.0) == (144, 3)  # 393 and 206 per arm do not fit 200; 144 over 3 months does


def test_the_shortest_length_that_fits_half_the_team_is_chosen():
    n2 = agenda.sample_size(10.0, 2.0, 0.05, 2)
    size = 2 * math.ceil(n2)  # exactly fits at two months
    assert agenda.holdback(10.0, 2.0, 0.05, size) == (math.ceil(n2), 2)
    assert agenda.holdback(10.0, 2.0, 0.05, size - 2)[1] == 3  # one person short at two months


def test_sigma_months_follow_the_pre_rollout_rule():
    base = agenda.fixture_inputs(row("np_all"))
    only_post = agenda.sigma_of(base)
    assert only_post == pytest.approx(row("np_all")["expected"]["sigma"], rel=1e-12)
    one_pre = replace(base, pre_levels=(20.0,), post_levels=(18.0, 22.0, 20.0))  # one pre month: all months are used
    assert agenda.sigma_of(one_pre) == pytest.approx(
        statistics.stdev([math.log(v) for v in (20.0, 18.0, 22.0, 20.0)]) * 20.0 * 10.0, rel=1e-12)
    two_pre = replace(base, pre_levels=(20.0, 22.0), post_levels=(50.0, 60.0))  # two pre months: only they are used
    assert agenda.sigma_of(two_pre) == pytest.approx(statistics.stdev([math.log(20.0), math.log(22.0)]) * 200.0)
    assert agenda.sigma_of(replace(base, pre_levels=(), post_levels=(5.0,))) is None
    assert agenda.sigma_of(replace(base, sigma_override=3.0)) == 3.0


def test_team_level_sigma_has_no_headcount_term_and_delta_uses_the_team_spend():
    r = agenda.fixture_inputs(row("pt_team_level"))
    assert agenda.sigma_of(r) == pytest.approx(statistics.stdev([10, 14, 11, 15, 12]))
    assert agenda.sigma_of(replace(r, team_size=300)) == agenda.sigma_of(r)
    assert agenda.breakeven(r) == pytest.approx(40.0 * 30 / 600.0)
    additive = replace(r, kind="additive")
    assert agenda.breakeven(additive) == pytest.approx(40.0 / 600.0)


def test_a_team_with_fewer_than_two_months_cannot_be_designed():
    r = replace(agenda.fixture_inputs(row("np_all")), pre_levels=(), post_levels=(5.0,))
    item = agenda.design(r)
    assert item.design == agenda.NOT_TESTABLE and item.sigma is None and "two months" in item.note


def test_pairing_uses_the_combined_headcount_for_capacity():
    r = agenda.fixture_inputs(row("nc_pair"))
    paired = agenda.design(r)
    assert paired.design == agenda.PAIR_STAGGER and (paired.n_per_arm, paired.length_months) == (32, 1)
    assert "80" in paired.note and "3 months" in paired.note
    alone = agenda.design(replace(r, pair_size=0))  # half of 30 is 15: 32 per arm does not fit, a longer test does
    assert alone.n_per_arm <= 15 and alone.length_months > paired.length_months


def test_wait_floor_and_the_two_sides_of_it():
    reach, floor, far = (agenda.design(agenda.fixture_inputs(row(i))) for i in ("tr_reach", "tr_floor", "tr_far"))
    assert reach.design == agenda.WAIT and reach.length_months == 2 and reach.n_per_arm is None
    t = stats.t.ppf(0.975, 2)
    # at m = 2 the half-width is under the target, at m = 1 it is not
    assert t * 0.02 * math.sqrt(6 / 10) <= reach.delta < t * 0.02 * math.sqrt(5 / 8)
    # the floor t sd / sqrt(2) is below the target for reach and far, above it for floor
    assert t * 0.02 / math.sqrt(2) < reach.delta and t * 0.02 / math.sqrt(2) < far.delta
    assert t * 0.05 / math.sqrt(2) > floor.delta
    assert floor.design == far.design == agenda.NOT_TESTABLE
    assert "does not shrink with waiting" in floor.note
    assert "does not reach" in far.note and "does not shrink" not in far.note
    assert "not testable alone either" in floor.note


def test_when_waiting_cannot_help_the_holdback_is_offered():
    r = agenda.fixture_inputs(row("tr_floor"))
    assert "not testable alone either" in agenda.design(r).note
    quiet = replace(r, sigma_override=2.0)  # a quiet outcome: 7 per arm in one month fits a team of 40
    item = agenda.design(quiet)
    assert item.design == agenda.NOT_TESTABLE
    assert "does not shrink with waiting; holdback instead: 7 per arm over 1 months" in item.note


def test_delta_log_by_direction():
    assert agenda.delta_log(3.0, 40.0, 1) == pytest.approx(math.log(1.075))
    assert agenda.delta_log(3.0, 30.0, -1) == pytest.approx(-math.log(0.9))
    assert agenda.delta_log(30.0, 30.0, -1) is None  # a lower-is-better team cannot improve by its whole baseline
    r = replace(agenda.fixture_inputs(row("tr_floor")), x0_cents=60000.0)  # delta = 30 = the baseline
    assert agenda.design(r).design == agenda.NOT_TESTABLE and "no usable" in agenda.design(r).note


def test_a_wait_item_without_cells_is_not_testable():
    r = replace(agenda.fixture_inputs(row("tr_reach")), cell_variances=())
    assert agenda.design(r).design == agenda.NOT_TESTABLE


def test_missing_value_per_unit_names_the_team():
    cfg = alloc.Config({"a": (1.0, 2.0, 3.0)}, None, 0.05, {})
    with pytest.raises(RoiError) as e:
        cfg.values("b")
    assert e.value.exit_code == 2 and "team b" in e.value.message


# ---- the command ------------------------------------------------------------------------------

def test_check_command_passes_on_the_fixture():
    out = CliRunner().invoke(main, ["agenda", "--check"])
    assert out.exit_code == 0, out.output
    assert "agenda: matches reference" in out.output
    assert out.output.count("ok   ") == len(ROWS) + 1


def test_check_json_report():
    report = json.loads(CliRunner().invoke(main, ["agenda", "--check", "--json"]).output)
    assert report["status"] == "pass" and len(report["checks"]) == len(ROWS) + 1


def test_check_fails_with_exit_1_on_a_wrong_expectation_or_a_missing_reason(tmp_path, monkeypatch):
    fixture = json.loads(FIXTURE.read_text())
    fixture["rows"][0]["expected"]["n_per_arm"] += 1
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(fixture))
    monkeypatch.setattr(agenda_command, "FIXTURE", bad)
    out = CliRunner().invoke(main, ["agenda", "--check"])
    assert out.exit_code == 1 and "FAIL np_all" in out.output

    fixture = json.loads(FIXTURE.read_text())
    fixture["rows"] = [r for r in fixture["rows"] if r["inputs"]["reason"] != "pre_trend"]
    bad.write_text(json.dumps(fixture))
    out = CliRunner().invoke(main, ["agenda", "--check"])
    assert out.exit_code == 1 and "no fixture row for pre_trend" in out.output


ROI_YAML = """
memo_budget_usd: 1000
rho: 0.05
sigma_override:
  alpha: 9.0
value_per_unit:
  alpha: {low: 5.0, base: 10.0, high: 15.0}
  beta: {low: 5.0, base: 10.0, high: 15.0}
  gamma: {low: 5.0, base: 10.0, high: 15.0}
"""


@pytest.fixture(scope="module")
def toy(tmp_path_factory):
    work = tmp_path_factory.mktemp("toy")
    (work / "config").mkdir()
    (work / "config" / "roi.yaml").write_text(ROI_YAML)
    out = CliRunner().invoke(main, ["--work-dir", str(work), "build", "--toy"])
    assert out.exit_code == 0, out.output
    return work


def test_the_toy_build_writes_an_agenda_item_for_the_unmeasured_team_only(toy):
    con = connect(str(toy / "warehouse" / "roi.duckdb"), read_only=True)
    items = con.execute("SELECT team_id, design, rho, baseline_basis, sigma FROM agenda_item").fetchall()
    effects = dict(con.execute("SELECT team_id, status FROM est_team_effect").fetchall())
    con.close()
    assert [i[0] for i in items] == [t for t, s in sorted(effects.items()) if s == "unmeasured"] == ["alpha"]
    team, design, rho, basis, sigma = items[0]
    assert (design, rho, basis, sigma) == ("holdback", 0.05, "pre", 9.0)  # sigma is the configured override


def test_the_agenda_command_prints_the_unmeasured_teams(toy):
    out = CliRunner().invoke(main, ["--work-dir", str(toy), "agenda"])
    assert out.exit_code == 0, out.output
    assert "alpha" in out.output and "beta" not in out.output and "gamma" not in out.output
    report = json.loads(CliRunner().invoke(main, ["--work-dir", str(toy), "agenda", "--json"]).output)
    assert [i["team_id"] for i in report["result"]["items"]] == ["alpha"]


def test_the_agenda_command_errors(toy, tmp_path):
    runner = CliRunner()
    assert runner.invoke(main, ["--work-dir", str(tmp_path), "agenda"]).exit_code == 2  # no config
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "roi.yaml").write_text(ROI_YAML)
    out = runner.invoke(main, ["--work-dir", str(tmp_path), "agenda"])
    assert out.exit_code == 2 and "run `roi build` first" in out.output
    assert runner.invoke(main, ["--work-dir", str(toy), "agenda", "--through", "2025-6"]).exit_code == 2
