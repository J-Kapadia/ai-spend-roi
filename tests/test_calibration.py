"""config/calibration.yaml: the schema always, and on a fixture the rule every row and effect must meet."""

import copy
from pathlib import Path

import pytest
import yaml

from roi import calibration
from roi.gen import config

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
FIXTURE = FIXTURES / "calibration_fixture.yaml"


def planted(gen: Path) -> dict[str, float]:
    """Planted effect of every team that has one (all but the control)."""
    return {t.team_id: t.effect_at_target for t in config.load(gen).teams if t.rollout_month is not None}


def fixture() -> dict:
    return copy.deepcopy(calibration.read(FIXTURE))


# ---- the real file: the schema, the rows, and every planted effect inside its range ---------------------

def test_the_real_calibration_file_matches_the_schema():
    raw = calibration.read(ROOT / "config" / "calibration.yaml")
    assert calibration.schema_problems(raw) == []


def test_the_real_file_has_a_verified_row_for_every_planted_effect_inside_its_range():
    path = ROOT / "config" / "calibration.yaml"
    raw = calibration.read(path)
    assert calibration.row_problems(raw) == []
    assert len(calibration.load(path)) == 7  # every team but the control
    assert calibration.effect_problems(raw, planted(ROOT / "config" / "gen.yaml")) == []


# ---- the fixture passes ----------------------------------------------------------------------------

def test_the_fixture_has_a_verified_row_for_every_planted_effect_inside_its_range():
    raw = calibration.read(FIXTURE)
    assert calibration.schema_problems(raw) == []
    assert calibration.row_problems(raw) == []
    effects = planted(FIXTURES / "gen_fixture.yaml")
    assert len(effects) == 7  # every team but the control
    assert calibration.effect_problems(raw, effects) == []
    assert len(calibration.load(FIXTURE)) == 7


# ---- and each way of failing is caught -------------------------------------------------------------

def test_an_empty_table_is_a_valid_schema():
    assert calibration.schema_problems({"rows": []}) == []


@pytest.mark.parametrize("raw", [{}, {"rows": None}, {"rows": {"a": 1}}, {"rows": [], "extra": 1}])
def test_a_wrong_shape_fails_the_schema(raw):
    assert calibration.schema_problems(raw)


def test_a_row_missing_a_key_or_with_an_unknown_key_fails_the_schema():
    raw = fixture()
    del raw["rows"][0]["figure"]
    raw["rows"][1]["note"] = "x"
    problems = calibration.schema_problems(raw)
    assert any("missing figure" in p for p in problems) and any("unknown key 'note'" in p for p in problems)


@pytest.mark.parametrize("value", [None, "", "13/10/2026", "2026-13-40", 20261013])
def test_a_row_without_a_usable_verified_on_fails(value):
    raw = fixture()
    raw["rows"][2]["verified_on"] = value
    assert any("verified_on" in p for p in calibration.row_problems(raw))


def test_an_unquoted_yaml_date_is_a_verified_on():
    raw = yaml.safe_load("rows:\n  - {team_id: a, effect_low: 0.1, effect_high: 0.2, source_ref: s, figure: f, verified_on: 2026-10-13, extrapolated: false}\n")
    assert calibration.row_problems(raw) == []


@pytest.mark.parametrize(
    "change",
    [
        {"effect_low": 0.3, "effect_high": 0.2},
        {"effect_low": None},
        {"effect_high": "0.2"},
        {"effect_low": True},
        {"effect_low": -1.0},
        {"source_ref": " "},
        {"figure": ""},
        {"extrapolated": "yes"},
        {"team_id": ""},
    ],
)
def test_a_bad_row_fails(change):
    raw = fixture()
    raw["rows"][0].update(change)
    assert calibration.row_problems(raw)


def test_a_duplicate_team_fails():
    raw = fixture()
    raw["rows"][1]["team_id"] = raw["rows"][0]["team_id"]
    assert any("duplicate" in p for p in calibration.row_problems(raw))


@pytest.mark.parametrize("team,effect", [("support", 0.1799 + 0.01), ("marketing", 0.19), ("finance_operations", 0.0)])
def test_a_planted_effect_outside_its_range_fails_and_names_the_team(team, effect):
    effects = planted(FIXTURES / "gen_fixture.yaml")
    effects[team] = effect
    problems = calibration.effect_problems(fixture(), effects)
    assert len(problems) == 1 and problems[0].startswith(team) and "outside the cited range" in problems[0]


def test_the_ends_of_a_range_are_inside_it():
    effects = planted(FIXTURES / "gen_fixture.yaml")
    effects["support"] = 0.10
    effects["marketing"] = 0.30
    assert calibration.effect_problems(fixture(), effects) == []


def test_a_team_with_no_row_and_a_row_with_no_team_fail():
    raw = fixture()
    raw["rows"].pop(0)
    raw["rows"].append({**raw["rows"][0], "team_id": "ghost_team"})
    problems = calibration.effect_problems(raw, planted(FIXTURES / "gen_fixture.yaml"))
    assert "platform_engineering: no calibration row" in problems
    assert any(p.startswith("ghost_team") for p in problems)


def test_load_refuses_a_file_that_fails_a_check(tmp_path):
    raw = fixture()
    raw["rows"][0]["verified_on"] = None
    path = tmp_path / "calibration.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="verified_on"):
        calibration.load(path)
