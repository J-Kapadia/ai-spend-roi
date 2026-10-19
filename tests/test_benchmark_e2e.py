"""The committed Ramp AI Index figures, end to end: from the reference file through `fct_benchmark_ramp` to a
number on the published page that `roi memo --check` and `roi explain` can resolve. The file is used as committed;
a memo month with no period at or before it must print the fallback sentence instead."""

import csv
import hashlib
import re
import shutil
from pathlib import Path

import pytest
from click.testing import CliRunner

from conftest import connect
from roi import memo
from roi.cli import main
from roi.workdir import WorkDir

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures"
REFERENCE = ROOT / "data" / "reference" / "ramp_ai_index.csv"
METRIC = "paid_adoption_share"
FALLBACK = "No published benchmark figure is available at or before"
TOY_MONTH = "2025-06"
FIXTURE_MONTH = "2026-03"


def roi(work, *args):
    return CliRunner().invoke(main, ["--work-dir", str(work), *args])


def reference_values() -> dict[str, str]:
    with REFERENCE.open(newline="", encoding="utf-8") as f:
        return {r["period"]: r["value"] for r in csv.DictReader(f) if r["metric"] == METRIC}


def expected_period(month: str) -> str | None:
    """The latest period of the committed file at or before `month`; None when there is none."""
    periods = [p for p in reference_values() if p <= month]
    return max(periods) if periods else None


def expected_text(month: str) -> str | None:
    period = expected_period(month)
    return None if period is None else f"{float(reference_values()[period]):.1f}%"


def build_toy(work):
    result = roi(work, "build", "--toy")
    assert result.exit_code == 0, result.output


def build_fixture(work):
    (work / "config").mkdir()
    shutil.copy(FIXTURES / "roi_fixture.yaml", work / "config" / "roi.yaml")
    result = roi(work, "build", "--config", str(FIXTURES / "gen_fixture.yaml"))
    assert result.exit_code == 0, result.output


COMPANIES = {"toy": (build_toy, TOY_MONTH), "fixture": (build_fixture, FIXTURE_MONTH)}


@pytest.fixture(scope="module", params=list(COMPANIES))
def company(request, tmp_path_factory):
    build, month = COMPANIES[request.param]
    work = tmp_path_factory.mktemp(f"benchmark_e2e_{request.param}")
    build(work)
    return work, month


@pytest.fixture
def copy(company, tmp_path):
    work, month = company
    shutil.copytree(work, tmp_path / "w", symlinks=True)
    return tmp_path / "w", month


def query(work, sql):
    con = connect(str(work / "warehouse" / "roi.duckdb"), read_only=True)
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


def site_html(work) -> str:
    return (work / "site" / "index.html").read_text(encoding="utf-8")


def section(work) -> str:
    html = site_html(work)
    start = html.index('<h2 id="benchmark">')
    return html[start:html.index("<p>Build facts", start)]


def benchmark_ids(work, month) -> dict[str, str]:
    con = memo.open_warehouse(WorkDir(work))
    try:
        _, collected = memo.collect(WorkDir(work), month, con)
    finally:
        con.close()
    return {r.name: r.prov_id for r in collected.records.values() if r.name.startswith("benchmark_")}


def test_the_mart_has_a_row_for_every_month_whose_period_exists(company):
    work, _ = company
    months = [m for (m,) in query(work, "SELECT DISTINCT strftime(month, '%Y-%m') FROM fct_team_month ORDER BY 1")]
    assert months
    got = query(work, f"SELECT strftime(month, '%Y-%m'), index_period, index_value FROM fct_benchmark_ramp"
                      f" WHERE metric = '{METRIC}' ORDER BY month")
    want = [(m, expected_period(m)) for m in months if expected_period(m) is not None]
    assert [(m, p) for m, p, _ in got] == want
    for _, period, value in got:
        assert float(value) == float(reference_values()[period])


def test_the_check_resolves_the_section_and_it_shows_the_committed_figure(company):
    work, month = company
    result = roi(work, "memo", "--month", month, "--check")
    assert result.exit_code == 0, result.output
    text = section(work)
    period, shown = expected_period(month), expected_text(month)
    if period is None:
        assert "benchmark:" not in result.output
        assert FALLBACK in text
        assert text.count("<p>") == 1
        assert benchmark_ids(work, month) == {}
        return
    assert re.search(r"^\s*benchmark: \d+ numbers? resolved", result.output, re.M), result.output
    assert re.search(rf"<[^>]*data-prov=[^>]*>\s*{re.escape(shown)}\s*</", text), text
    assert f'<time datetime="{period}">' in text
    assert FALLBACK not in text


def test_explain_resolves_every_benchmark_number_to_the_mart(company):
    work, month = company
    ids = benchmark_ids(work, month)
    if expected_period(month) is None:
        assert ids == {}
        return
    assert "benchmark_index" in ids
    for name, pid in ids.items():
        result = roi(work, "explain", pid)
        assert result.exit_code == 0, (name, result.output)
        assert "source: fct_benchmark_ramp where" in result.output


def test_two_renders_are_byte_identical(copy):
    work, month = copy
    assert roi(work, "memo", "--month", month).exit_code == 0
    first = hashlib.sha256((work / "site" / "index.html").read_bytes()).hexdigest()
    prov_pages = sorted((work / "site").rglob("*.html"))
    pages = {p: p.read_bytes() for p in prov_pages}
    assert roi(work, "memo", "--month", month).exit_code == 0
    assert hashlib.sha256((work / "site" / "index.html").read_bytes()).hexdigest() == first
    assert {p: p.read_bytes() for p in sorted((work / "site").rglob("*.html"))} == pages


def nudge(value: str) -> str:
    """The value moved by a ten-thousandth, the direction that leaves its one-decimal display unchanged: the
    memo then still finds the stored record of the number and the check has to catch the changed row."""
    for step in (0.0001, -0.0001):
        moved = f"{float(value) + step:.4f}"
        if f"{float(moved):.1f}" == f"{float(value):.1f}":
            return moved
    raise AssertionError(f"no nudge of {value} keeps its display")


def test_a_changed_index_value_in_the_mart_fails_the_check(copy):
    work, month = copy
    period = expected_period(month)
    if period is None:
        pytest.skip("the committed file has no period at or before the memo month")
    moved = nudge(reference_values()[period])
    con = connect(str(work / "warehouse" / "roi.duckdb"))
    try:
        con.execute(f"UPDATE fct_benchmark_ramp SET index_value = {moved}"
                    f" WHERE month = DATE '{month}-01' AND metric = '{METRIC}'")
    finally:
        con.close()
    result = roi(work, "memo", "--month", month, "--check")
    assert result.exit_code == 1, result.output
    assert "checksum mismatch" in result.output
