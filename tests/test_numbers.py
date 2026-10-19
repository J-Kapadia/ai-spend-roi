"""`roi.numbers`: the one enumeration of the numbers a memo prints, tested on the fixture company."""

import hashlib
import re
import shutil
from pathlib import Path

import pytest
from click.testing import CliRunner

from conftest import connect
from roi import memo, prov
from roi.cli import main
from roi.numbers import Number, by_name, enumerate_numbers

FIXTURES = Path(__file__).parent / "fixtures"
THROUGH = "2026-03"
# The memo HTML of the fixture company's build, whose benchmark section shows the repository's Ramp AI Index table and whose return section carries the caveats and the pre-trend glossary.
MEMO_SHA256 = "98f1992ab5076edb73dc69bfb6f9e8b35b1e30c8b25cb09bd9a2d2c6526a9ee1"


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    work = tmp_path_factory.mktemp("numbers")
    (work / "config").mkdir()
    shutil.copy(FIXTURES / "roi_fixture.yaml", work / "config" / "roi.yaml")
    runner = CliRunner()
    result = runner.invoke(main, ["--work-dir", str(work), "build", "--config", str(FIXTURES / "gen_fixture.yaml")])
    assert result.exit_code == 0, result.output
    result = runner.invoke(main, ["--work-dir", str(work), "memo", "--month", THROUGH])
    assert result.exit_code == 0, result.output
    return work


@pytest.fixture(scope="module")
def numbers(built) -> list[Number]:
    con = connect(str(built / "warehouse" / "roi.duckdb"), read_only=True)
    try:
        return enumerate_numbers(con, THROUGH, built / "config" / "roi.yaml")
    finally:
        con.close()


def test_the_ids_of_the_enumeration_are_the_ids_in_the_memo(built, numbers):
    ids = {prov.make_id(n.name, THROUGH, n.source_model, n.source_predicate, n.display_value) for n in numbers}
    html = (built / "site" / "index.html").read_text(encoding="utf-8")
    assert ids == set(re.findall(r'data-prov="([0-9a-f]{16})"', html))


def test_two_calls_give_identical_lists(built, numbers):
    con = connect(str(built / "warehouse" / "roi.duckdb"), read_only=True)
    try:
        assert enumerate_numbers(con, THROUGH, built / "config" / "roi.yaml") == numbers
    finally:
        con.close()


def test_names_are_unique(numbers):
    assert len(by_name(numbers)) == len(numbers)


def test_every_predicate_is_bounded_at_the_month(numbers):
    for n in numbers:
        if n.source_model == prov.CONFIG_MODEL:
            continue
        assert f"through_month = '{THROUGH}'" in n.source_predicate or f"month = DATE '{THROUGH}-01'" in n.source_predicate, n


def test_config_numbers_have_the_config_model(numbers):
    assumptions = [n for n in numbers if n.section == "assumptions"]
    assert assumptions
    assert all(n.source_model == "config" for n in assumptions)
    assert all(n.source_model != "config" for n in numbers if n.section != "assumptions")


def test_the_headline_comes_first_and_every_section_appears(numbers):
    assert numbers[0].name == "company_ai_spend_usd" and numbers[0].section == "headline"
    assert {n.section for n in numbers} == {"headline", "return", "allocation", "agenda", "assumptions", "benchmark"}


def test_the_memo_html_is_the_one_the_numbers_replaced(built):
    assert hashlib.sha256((built / "site" / "index.html").read_bytes()).hexdigest() == MEMO_SHA256


def test_collect_prints_what_was_enumerated(built, numbers):
    from roi.workdir import WorkDir

    con = memo.open_warehouse(WorkDir(built))
    try:
        _, collected = memo.collect(WorkDir(built), THROUGH, con)
    finally:
        con.close()
    assert {r.display_value for r in collected.records.values()} <= {n.display_value for n in numbers}
