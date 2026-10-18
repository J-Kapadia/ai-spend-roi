"""scripts/design-report.py: the report's tables hold the numbers of `roi.design`, `--out` writes
what stdout prints, and a missing config exits 2."""

import re
import subprocess
import sys
from pathlib import Path

import pytest

from roi import design
from roi.gen import config

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = "scripts/design-report.py"
FIXTURE = (ROOT / "tests" / "fixtures" / "gen_fixture.yaml", ROOT / "tests" / "fixtures" / "roi_fixture.yaml")
SHIPPED = (ROOT / "config" / "gen.yaml", ROOT / "config" / "roi.yaml")
TIMEOUT = 300


def run(*args, cwd=ROOT):
    return subprocess.run([sys.executable, SCRIPT, *map(str, args)], cwd=cwd, capture_output=True, text=True, timeout=TIMEOUT)


@pytest.fixture(scope="module", params=[FIXTURE, SHIPPED], ids=["fixture", "shipped"])
def paths(request):
    return request.param


@pytest.fixture(scope="module")
def cfg(paths):
    return config.load(paths[0])


@pytest.fixture(scope="module")
def result(paths):
    gen, roi = paths
    return run("--config", gen, "--roi", roi)


def design_rows(text):
    """{(through, team): cells} for the rows of the 'Teams measured through' tables."""
    rows, through = {}, None
    for line in text.splitlines():
        heading = re.match(r"## Teams measured through (\S+)", line)
        if heading:
            through = heading.group(1)
        elif through and line.startswith("| ") and not line.startswith(("| team", "|---")):
            cells = [c.strip() for c in line.strip("|").split("|")]
            rows[(through, cells[0])] = cells
        elif line.startswith("## "):
            through = None
    return rows


def test_exits_zero_and_every_team_appears(result, cfg):
    assert result.returncode == 0, result.stderr
    for spec in cfg.teams:
        assert spec.team_id in result.stdout, spec.team_id


def test_each_printed_standard_error_equals_the_design_function(result, cfg):
    rows = design_rows(result.stdout)
    assert {t for _, t in rows} >= set(design.MEASURED_AT_LAST) | set(design.MEASURED_AT_INCOMING)
    assert {m for m, _ in rows} == {cfg.last_month, cfg.incoming_month}
    for (through, team), cells in rows.items():
        d = design.team_design(cfg, team, through)
        assert cells[5] == f"{design.team_effect_se(cfg, team, through):.4f}", (through, team)
        assert cells[1:3] == [str(d.n_pre), str(d.n_post)], (through, team)
        assert cells[6] == str(d.df) and cells[7] == f"{d.t_mult:.3f}", (through, team)


def test_the_other_sections_are_present(result, cfg):
    for heading in ("## Noise solution", "### Iteration", "### Converged values", "## Dose-response curve at design",
                    "## Value per unit", "## Memo budget"):
        assert heading in result.stdout, heading
    for team in design.MEASURED_AT_LAST:
        assert re.search(rf"^\| {team} \| {cfg.last_month} \| \S+ \| (yes|no) \|$", result.stdout, re.M), team
    value_rows = [line for line in result.stdout.split("## Value per unit")[1].split("## Memo budget")[0].splitlines()
                  if line.startswith("| ") and not line.startswith(("| team", "|---"))]
    assert len(value_rows) == len(cfg.teams)
    assert all(row.endswith("|  |") for row in value_rows)  # reasoning left blank


def test_the_memo_budget_check_matches_on_the_shipped_config():
    out = run("--config", SHIPPED[0], "--roi", SHIPPED[1]).stdout
    assert "(matches the formula)" in out


def test_out_writes_the_same_text_as_stdout(paths, result, tmp_path):
    gen, roi = paths
    target = tmp_path / "report.md"
    written = run("--config", gen, "--roi", roi, "--out", target)
    assert written.returncode == 0, written.stderr
    assert written.stdout == ""
    assert target.read_text(encoding="utf-8") == result.stdout


@pytest.mark.parametrize("missing", ["config", "roi"])
def test_a_missing_file_exits_two(missing, tmp_path):
    gen, roi = FIXTURE
    args = {"config": gen, "roi": roi, missing: tmp_path / "absent.yaml"}
    result = run("--config", args["config"], "--roi", args["roi"])
    assert result.returncode == 2
    assert "absent.yaml" in result.stderr
    assert result.stdout == ""
