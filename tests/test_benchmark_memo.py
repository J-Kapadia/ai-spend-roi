"""The benchmark section of the memo: the published share of businesses paying for AI tools, beside the company's own."""

import re
import shutil

import pytest
from click.testing import CliRunner

from conftest import connect
from roi import memo
from roi.cli import main
from roi.steps import BuildContext, provenance
from roi.workdir import WorkDir

MONTH = "2025-06"
PREDICATE = "month = DATE '2025-06-01' AND metric = 'paid_adoption_share'"
DDL = (
    "CREATE TABLE IF NOT EXISTS fct_benchmark_ramp(month DATE, metric VARCHAR, index_period VARCHAR,"
    " index_value DECIMAL(18,4), index_unit VARCHAR, prior_period VARCHAR, prior_value DECIMAL(18,4),"
    " source_url VARCHAR, retrieved_on DATE, teams INTEGER, teams_with_ai INTEGER, company_share DECIMAL(18,9))"
)
ROW = (
    "INSERT INTO fct_benchmark_ramp VALUES (DATE '2025-06-01', 'paid_adoption_share', '2025-05', 44.4, 'percent',"
    " '2024-05', {prior}, 'https://ramp.com/fixture', DATE '2026-10-18', 3, 2, 0.666666667)"
)
NUMBERS = ["benchmark_index", "benchmark_prior", "benchmark_teams_with_ai", "benchmark_teams", "benchmark_company_share"]
FALLBACK = "No published benchmark figure is available at or before"


def roi(work, *args):
    return CliRunner().invoke(main, ["--work-dir", str(work), *args])


def edit(work, *statements):
    con = connect(str(work / "warehouse" / "roi.duckdb"))
    try:
        for sql in statements:
            con.execute(sql)
    finally:
        con.close()


def scan(work) -> memo._MemoScan:
    s = memo._MemoScan()
    s.feed((work / "site" / "index.html").read_text(encoding="utf-8"))
    return s


def section(work) -> str:
    html = (work / "site" / "index.html").read_text(encoding="utf-8")
    start = html.index('<h2 id="benchmark">')
    return html[start:html.index("<p>Build facts", start)]


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    work = tmp_path_factory.mktemp("benchmark")
    result = roi(work, "build", "--toy")
    assert result.exit_code == 0, result.output
    return work


@pytest.fixture
def copy(built, tmp_path):
    shutil.copytree(built, tmp_path / "w", symlinks=True)
    return tmp_path / "w"


def with_row(work, prior="30.1"):
    # The build loads the mart from the repository's reference table; the row is replaced by hand in a copy
    # so its figures are known, the records the build stored for the loaded row are dropped (the provenance
    # step keeps a record whose id it already has), and the step is run again to store those of the new row.
    edit(
        work, DDL, "DELETE FROM fct_benchmark_ramp", ROW.format(prior=prior),
        "DELETE FROM prov_record WHERE name LIKE 'benchmark\\_%' ESCAPE '\\'",
    )
    db = work / "warehouse" / "roi.duckdb"
    provenance.run(BuildContext(work=WorkDir(work), db=db, through=MONTH))
    result = roi(work, "memo", "--month", MONTH, "--check")
    assert result.exit_code == 0, result.output
    return result


@pytest.fixture
def loaded(copy):
    return copy, with_row(copy)


def benchmark_ids(work) -> dict[str, str]:
    con = memo.open_warehouse(WorkDir(work))
    try:
        _, c = memo.collect(WorkDir(work), MONTH, con)
    finally:
        con.close()
    return {r.name: r.prov_id for r in c.records.values() if r.name.startswith("benchmark_")}


def test_the_check_resolves_five_benchmark_numbers(loaded):
    work, result = loaded
    assert "benchmark: 5 numbers resolved" in result.output


def test_the_section_shows_the_figures_tagged_with_a_provenance_page(loaded):
    work, _ = loaded
    text = section(work)
    assert "Ramp AI Index" in text
    s = scan(work)
    ids = benchmark_ids(work)
    assert list(ids) == NUMBERS
    tagged = {t["id"]: t for t in s.tagged}
    assert [tagged[ids[name]]["text"] for name in NUMBERS] == ["44.4%", "30.1%", "2", "3", "67%"]
    for name in NUMBERS:
        tag = tagged[ids[name]]
        assert tag["href"] == f"prov/{ids[name]}.html"
        assert (work / "site" / "prov" / f"{ids[name]}.html").exists()


def test_explain_prints_the_mart_row_of_every_benchmark_number(loaded):
    work, _ = loaded
    for name, pid in benchmark_ids(work).items():
        result = roi(work, "explain", pid)
        assert result.exit_code == 0, (name, result.output)
        assert f"source: fct_benchmark_ramp where {PREDICATE}" in result.output


def digits_outside_time(text: str) -> list[str]:
    """Numerals left in the section once its tags and its `<time>` elements are removed."""
    text = re.sub(r"<time[^>]*>.*?</time>", "", text)
    text = re.sub(r"<[^>]+>", "", text)
    return [m.group(0) for m in memo.NUMBER.finditer(text)]


def test_no_untagged_and_no_literal_number_in_the_section(loaded):
    work, _ = loaded
    s = memo._MemoScan()
    s.feed(f"<body>{section(work)}</body>")
    assert not s.untagged and not s.lit
    assert sorted(t["id"] for t in s.tagged) == sorted(benchmark_ids(work).values())
    assert sorted(digits_outside_time(section(work))) == sorted(t["text"] for t in s.tagged)


def test_the_section_follows_the_calibration_and_the_questions_keep_their_order(loaded):
    work, _ = loaded
    headings = scan(work).headings
    assert [h for h in headings if h in memo.QUESTIONS] == list(memo.QUESTIONS)
    assert headings[-1] == "How this compares with a published benchmark"
    html = (work / "site" / "index.html").read_text(encoding="utf-8")
    assert html.index('<h2 id="benchmark">') < html.index("<p>Build facts")
    assert html.index('<h2 id="test-next">') < html.index('<h2 id="benchmark">')


def test_two_renders_are_byte_identical(loaded):
    work, _ = loaded
    first = (work / "site" / "index.html").read_bytes()
    assert roi(work, "memo", "--month", MONTH).exit_code == 0
    assert (work / "site" / "index.html").read_bytes() == first


def test_a_null_prior_drops_the_prior_clause_and_one_number(copy):
    result = with_row(copy, prior="NULL")
    assert "benchmark: 4 numbers resolved" in result.output
    assert "twelve months earlier" not in section(copy)
    assert list(benchmark_ids(copy)) == [n for n in NUMBERS if n != "benchmark_prior"]


def assert_fallback(work):
    result = roi(work, "memo", "--month", MONTH, "--check")
    assert result.exit_code == 0, result.output
    assert "benchmark:" not in result.output
    text = section(work)
    assert FALLBACK in text
    assert not scan(work).untagged
    assert not digits_outside_time(text)
    assert text.count("<p>") == 1


def test_a_month_without_a_row_says_so_in_one_sentence(copy):
    edit(copy, DDL, "DELETE FROM fct_benchmark_ramp")
    assert roi(copy, "memo", "--month", MONTH).exit_code == 0
    assert_fallback(copy)


def test_a_missing_table_says_so_in_one_sentence(copy):
    edit(copy, "DROP TABLE IF EXISTS fct_benchmark_ramp")
    assert roi(copy, "memo", "--month", MONTH).exit_code == 0
    assert_fallback(copy)


def test_the_toy_memo_resolves_the_benchmark_numbers_of_the_loaded_table(built):
    result = roi(built, "memo", "--month", MONTH, "--check")
    assert result.exit_code == 0, result.output
    assert "headline: 1 number resolved" in result.output and "benchmark: 5 numbers resolved" in result.output
