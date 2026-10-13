"""The group-time estimator against the independent reference and the hand-worked fixtures."""

import csv
import hashlib
import json
import math
from datetime import date
from pathlib import Path

import duckdb
import numpy as np
import pytest
from click.testing import CliRunner

from reference import cs_reference as ref
from roi import estimator
from roi.cli import main
from roi.steps import BuildContext
from roi.steps import estimate as estimate_step
from roi.workdir import WorkDir

FIXTURES = Path(__file__).resolve().parent / "fixtures"
TOL = 1e-6
MEASURED_SMALL = ("team_a", "team_b", "team_c")


def load(name, drop_months=()):
    y, outcome, rollout = {}, {}, {}
    with (FIXTURES / name).open(newline="") as f:
        for row in csv.DictReader(f):
            if row["month"] in drop_months:
                continue
            y.setdefault(row["team_id"], {})[row["month"]] = float(row["y_log"])
            outcome.setdefault(row["team_id"], {})[row["month"]] = float(row["outcome"])
            rollout[row["team_id"]] = row["rollout_month"] or None
    return y, rollout, outcome


def panel_of(name, **kwargs):
    y, rollout, outcome = load(name, **kwargs)
    return estimator.Panel(y, rollout, outcome)


def last_month(name):
    return max(m for team in load(name)[0].values() for m in team)


@pytest.fixture(scope="module")
def expected():
    return json.loads((FIXTURES / "panel_small.expected.json").read_text())


@pytest.fixture(scope="module")
def small():
    return estimator.estimate(panel_of("panel_small.csv"), "2025-10")


def close(a, b):
    assert a == pytest.approx(b, abs=TOL, rel=0), (a, b)


# --- the estimator equals the reference and the hand-worked values -----------------------------


def test_every_quantity_equals_the_reference(small):
    y, rollout, _ = load("panel_small.csv")
    r = ref.reference(y, rollout, "2025-10")
    for team in MEASURED_SMALL:
        got, want = small[team].analysis, r[team]
        assert small[team].status == want["status"] == "measured"
        assert [c.t for c in got.cells] == [c["t"] for c in want["cells"]]
        for c, w in zip(got.cells, want["cells"]):
            close(c.att, w["att"])
            close(c.var, w["var"])
            close(c.se, math.sqrt(w["var"]))
            assert c.n_controls == w["n_controls"]
        np.testing.assert_allclose(got.cov, np.array(want["cov"]), atol=TOL, rtol=0)
        assert got.df == want["df"]
        close(got.t_mult, want["t_mult"])
        close(got.effect, want["effect"])
        close(got.se, want["se"])
        close(got.ci_lo, want["ci"][0])
        close(got.ci_hi, want["ci"][1])
        close(got.pretrend.statistic, want["lead_stat"])
        close(got.pretrend.p_value, want["lead_p"])
    assert small["team_n1"].status == small["team_n2"].status == "control"


def test_every_quantity_equals_the_hand_worked_values(small, expected):
    assert expected["through"] == "2025-10"
    for team, want in expected["teams"].items():
        got = small[team].analysis
        assert [c.n_controls for c in got.cells] == [c["n_controls"] for c in want["cells"]]
        for c, w in zip(got.cells, want["cells"]):
            close(c.att, w["att"])
            close(c.var, w["var"])
            close(c.se, w["se"])
        np.testing.assert_allclose(got.cov, np.array(want["cov"]), atol=TOL, rtol=0)
        assert got.df == want["df"]
        close(got.t_mult, want["t_mult"])
        close(got.effect, want["effect"])
        close(small[team].effect, want["effect"])
        close(got.se, want["se"])
        close(small[team].ci_lo, want["ci_lo"])
        close(small[team].ci_hi, want["ci_hi"])
        close(got.pretrend.statistic, want["lead_stat"])
        close(got.pretrend.p_value, want["lead_p"])
        np.testing.assert_allclose([c.att for c in got.pretrend.leads], want["lead_att"], atol=TOL, rtol=0)
        np.testing.assert_allclose(got.pretrend.cov, np.array(want["lead_cov"]), atol=TOL, rtol=0)


def test_reference_equals_the_hand_worked_values(expected):
    y, rollout, _ = load("panel_small.csv")
    r = ref.reference(y, rollout, "2025-10")
    for team, want in expected["teams"].items():
        close(r[team]["effect"], want["effect"])
        close(r[team]["se"], want["se"])
        close(r[team]["ci"][0], want["ci_lo"])
        close(r[team]["ci"][1], want["ci_hi"])


def test_a_pair_of_cells_with_different_control_sets(small):
    """ATT(m4, m4) uses four controls, ATT(m4, m5) three: the covariance is not half a variance."""
    cells = small["team_a"].analysis.cells
    assert [c.n_controls for c in cells[:3]] == [4, 3, 2]
    assert set(cells[0].controls) - set(cells[1].controls) == {"team_b"}
    cov = small["team_a"].analysis.cov
    close(cov[0, 1], 5.085e-05)
    assert cov[0, 1] != pytest.approx(0.5 * cells[0].var, rel=0.2)


def test_sigma_is_positive_semi_definite_on_every_fixture():
    for name in ("panel_small.csv", "panel_pretrend.csv", "panel_nocomparison.csv"):
        panel = panel_of(name)
        for team, result in estimator.estimate(panel, last_month(name)).items():
            a = result.analysis
            if a is None:
                continue
            assert np.linalg.eigvalsh(a.cov).min() >= -1e-12, (name, team)
            assert np.allclose(a.cov, a.cov.T)
            if a.pretrend.testable:
                assert np.linalg.eigvalsh(a.pretrend.cov).min() > 0


# --- the naive two-way fixed-effects fit is biased where the estimator is not -----------------


def test_twfe_is_biased_on_the_fixture_and_the_estimator_is_not(small):
    y, rollout, _ = load("panel_small.csv")
    # Planted effect 0.04k, 0.025k, 0.01k at k months since rollout (panel_small.md).
    planted = [th * k for th, g in ((0.04, 4), (0.025, 5), (0.01, 6)) for k in range(1, 10 - g + 2)]
    true_avg = sum(planted) / len(planted)
    twfe = ref.twfe_coefficient(y, rollout)
    assert abs(twfe - true_avg) / true_avg > 0.25, (twfe, true_avg)
    # The estimator's team effects stay within 1.5 standard errors of the planted team averages.
    for team, th, g in (("team_a", 0.04, 4), ("team_b", 0.025, 5), ("team_c", 0.01, 6)):
        n = 10 - g + 1
        planted_team = th * (n + 1) / 2
        a = small[team].analysis
        assert abs(a.effect - planted_team) < 1.5 * a.se


def test_two_scale_fixture_is_scale_free_on_the_log_scale(small):
    y, rollout, outcome = load("panel_small.csv")
    a_level, n2_level = np.mean(list(outcome["team_a"].values())), np.mean(list(outcome["team_n2"].values()))
    assert a_level / n2_level >= 1000
    # Rescaling a team's outcome by 1000 shifts its Y by ln(1000) and leaves its effect alone.
    shifted = {t: dict(v) for t, v in y.items()}
    shifted["team_n2"] = {m: v + math.log(1000) for m, v in y["team_n2"].items()}
    shifted["team_b"] = {m: v - math.log(1000) for m, v in y["team_b"].items()}
    again = estimator.estimate(estimator.Panel(shifted, rollout), "2025-10")
    for team in MEASURED_SMALL:
        close(again[team].effect, small[team].effect)
    # A difference of levels does not cancel a multiplicative shock: team_a's level moves by its
    # own scale times the shock, the controls' by theirs, and the gap reads as a large effect.
    cell = small["team_a"].analysis.cells[0]
    controls = cell.controls
    level_did = (
        (outcome["team_a"]["2025-04"] - outcome["team_a"]["2025-03"])
        - np.mean([outcome[c]["2025-04"] - outcome[c]["2025-03"] for c in controls])
    ) / outcome["team_a"]["2025-03"]
    assert level_did > 3 * cell.att > 0


# --- statuses ---------------------------------------------------------------------------------


def test_pretrend_fixture_is_flagged_and_reports_no_figures():
    name = "panel_pretrend.csv"
    result = estimator.estimate(panel_of(name), last_month(name))
    x = result["team_x"]
    assert (x.status, x.reason) == ("unmeasured", "pre_trend")
    assert x.effect is x.ci_lo is x.ci_hi is x.df is None
    assert x.analysis.pretrend.testable and x.analysis.pretrend.p_value < 0.10
    assert result["team_p"].status == "measured"
    y, rollout, _ = load(name)
    r = ref.reference(y, rollout, last_month(name))
    assert (r["team_x"]["status"], r["team_x"]["reason"]) == ("unmeasured", "pre_trend")
    close(x.analysis.pretrend.statistic, r["team_x"]["lead_stat"])
    close(x.analysis.pretrend.p_value, r["team_x"]["lead_p"])
    assert x.analysis.pretrend.statistic > 5.342644  # the 90% point of F(4, 3)


def test_nocomparison_fixture_is_unmeasured_for_every_team():
    name = "panel_nocomparison.csv"
    result = estimator.estimate(panel_of(name), last_month(name))
    assert {t: (r.status, r.reason) for t, r in result.items()} == {
        t: ("unmeasured", "no_comparison") for t in ("team_p", "team_q", "team_r")
    }
    assert all(r.effect is None for r in result.values())
    assert [len(r.analysis.cells) if r.analysis else 0 for r in result.values()] == [2, 1, 0]


@pytest.mark.parametrize("scale", [-0.5, 0.0])
def test_non_positive_variance_is_unmeasured_never_a_zero_width_interval(monkeypatch, scale):
    name = "panel_small.csv"
    panel, through = panel_of(name), last_month(name)
    real = estimator._cov_matrix
    # w' Sigma w = scale * (mean of the diagonal) / n_cells: negative, then exactly zero
    monkeypatch.setattr(
        estimator, "_cov_matrix", lambda cells, series: scale * np.eye(len(cells)) * float(np.mean(np.diag(real(cells, series))))
    )
    result = estimator.estimate(panel, through)
    for team in MEASURED_SMALL:
        r = result[team]
        assert (r.status, r.reason) == ("unmeasured", "variance_not_computable")
        assert r.effect is r.ci_lo is r.ci_hi is r.df is None
        assert not r.analysis.computable and math.isnan(r.analysis.se)


def test_too_recent_is_a_status_only_expectation():
    result = estimator.estimate(panel_of("panel_small.csv"), "2025-06")
    assert result["team_a"].status == "measured"
    for team in ("team_b", "team_c"):
        assert (result[team].status, result[team].reason) == ("unmeasured", "too_recent")
        assert result[team].effect is None
    assert result["team_b"].n_post == 2 and result["team_c"].n_post == 1


def test_no_pre_period_is_a_status_only_expectation():
    result = estimator.estimate(panel_of("panel_small.csv", drop_months=("2025-01", "2025-02")), "2025-10")
    assert (result["team_a"].status, result["team_a"].reason) == ("unmeasured", "no_pre_period")
    assert result["team_a"].n_pre == 1 and result["team_a"].effect is None
    # team_b keeps two pre months (m3, m4) and is short too; team_c keeps three (m3 to m5) and is measured.
    assert (result["team_b"].status, result["team_b"].reason) == ("unmeasured", "no_pre_period")
    assert result["team_c"].status == "measured"


def test_a_team_rolled_out_after_the_window_is_too_recent():
    result = estimator.estimate(panel_of("panel_small.csv"), "2025-03")
    assert {t: r.reason for t, r in result.items() if r.status == "unmeasured"} == {
        t: "too_recent" for t in MEASURED_SMALL
    }


def test_untestable_pretrend_leaves_the_team_measured():
    """A clean linear drift makes Sigma_pre non-positive-definite; the team stays measured, untested."""
    y, rollout, outcome = load("panel_small.csv")
    for m in ("2025-01", "2025-02", "2025-03"):
        y["team_a"][m] += 0.1 * int(m[5:])
    result = estimator.estimate(estimator.Panel(y, rollout, outcome), "2025-10")["team_a"]
    assert result.status == "measured"
    assert not result.analysis.pretrend.testable and result.analysis.pretrend.p_value is None
    assert ref.reference(y, rollout, "2025-10")["team_a"]["lead_testable"] is False


def test_baseline_uses_pre_months_and_falls_back_to_all():
    panel = panel_of("panel_small.csv")
    result = estimator.estimate(panel, "2025-10")
    pre = [v for m, v in panel.outcome["team_a"].items() if m < "2025-04"]
    assert (result["team_a"].baseline_basis, result["team_a"].baseline_outcome) == ("pre", pytest.approx(np.mean(pre)))
    assert result["team_n1"].baseline_basis == "all"
    # A team with no pre-rollout month at all (rollout in the first month) uses all its months.
    y, rollout, outcome = load("panel_small.csv")
    rollout["team_b"] = "2025-01"
    r = estimator.estimate(estimator.Panel(y, rollout, outcome), "2025-10")["team_b"]
    assert (r.status, r.reason, r.baseline_basis) == ("unmeasured", "no_pre_period", "all")


# --- the build step and the command ------------------------------------------------------------


def make_warehouse(path, name):
    y, rollout, outcome = load(name)
    con = duckdb.connect(str(path))
    con.execute(
        "CREATE TABLE fct_team_month(team_id VARCHAR, month DATE, outcome DOUBLE, y_log DOUBLE,"
        " spend_per_head_cents BIGINT, headcount INTEGER, rollout_month DATE)"
    )
    rows = []
    for team in y:
        for m in y[team]:
            g = rollout[team]
            rows.append((team, date(int(m[:4]), int(m[5:]), 1), outcome[team][m], y[team][m], 0, 4,
                         date(int(g[:4]), int(g[5:]), 1) if g else None))
    con.executemany("INSERT INTO fct_team_month VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
    con.close()


def test_step_writes_the_result_tables_with_no_figures_for_unmeasured_teams(tmp_path):
    work = WorkDir(tmp_path)
    work.warehouse.mkdir()
    make_warehouse(work.warehouse_next_db, "panel_pretrend.csv")
    estimate_step.run(BuildContext(work=work, db=work.warehouse_next_db, through="2025-10"))
    con = duckdb.connect(str(work.warehouse_next_db), read_only=True)
    eff = {r[0]: r for r in con.execute(
        "SELECT team_id, status, reason, effect, ci_lo, ci_hi, n_pre, n_post, df, baseline_basis, estimator_version,"
        " through_month FROM est_team_effect").fetchall()}
    assert eff["team_x"][1:6] == ("unmeasured", "pre_trend", None, None, None)
    assert eff["team_p"][1] == "measured" and eff["team_p"][3] < eff["team_p"][5] and eff["team_p"][8] == 4
    assert eff["team_n1"][1] == "control"
    assert {r[10] for r in eff.values()} == {estimator.ESTIMATOR_VERSION} and {r[11] for r in eff.values()} == {"2025-10"}
    cells = con.execute("SELECT DISTINCT team_id FROM est_att_gt").fetchall()
    assert cells == [("team_p",)]
    assert con.execute("SELECT count(*) FROM est_att_gt WHERE team_id = 'team_p'").fetchone()[0] == eff["team_p"][7]
    n = eff["team_p"][7]
    assert con.execute("SELECT count(*) FROM est_att_cov").fetchone()[0] == n * (n + 1) // 2
    assert con.execute("SELECT count(*) FROM est_att_cov WHERE t1 = t2").fetchone()[0] == n
    diag, se = con.execute(
        "SELECT c.cov, a.se * a.se FROM est_att_cov c JOIN est_att_gt a ON a.team_id = c.team_id AND a.t = c.t1"
        " WHERE c.t1 = c.t2 LIMIT 1").fetchone()
    assert diag == pytest.approx(se, abs=1e-15)
    con.close()


def test_estimate_command_prints_the_team_effects_and_does_not_touch_the_warehouse(tmp_path):
    work = WorkDir(tmp_path)
    work.warehouse.mkdir()
    make_warehouse(work.warehouse_db, "panel_small.csv")
    before = hashlib.sha256(work.warehouse_db.read_bytes()).hexdigest()
    runner = CliRunner()
    table = runner.invoke(main, ["--work-dir", str(tmp_path), "estimate"])
    assert table.exit_code == 0, table.output
    assert "team_a" in table.output and "measured" in table.output and "control" in table.output
    out = runner.invoke(main, ["--work-dir", str(tmp_path), "estimate", "--through", "2025-06", "--json"])
    assert out.exit_code == 0, out.output
    data = json.loads(out.output)
    assert data["through_month"] == "2025-06"
    assert data["teams"]["team_a"]["status"] == "measured" and data["teams"]["team_b"]["reason"] == "too_recent"
    assert data["teams"]["team_b"]["effect"] is None
    full = json.loads(runner.invoke(main, ["--work-dir", str(tmp_path), "estimate", "--json"]).output)
    close(full["teams"]["team_c"]["effect"], 0.0553)
    assert hashlib.sha256(work.warehouse_db.read_bytes()).hexdigest() == before


def test_estimate_command_errors(tmp_path):
    runner = CliRunner()
    missing = runner.invoke(main, ["--work-dir", str(tmp_path), "estimate"])
    assert missing.exit_code == 2 and "roi build" in missing.output
    bad = runner.invoke(main, ["--work-dir", str(tmp_path), "estimate", "--through", "2025-13"])
    assert bad.exit_code == 2
