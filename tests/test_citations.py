"""`config/citations.yaml`: the memo quotes a public statement only from a verified row, and no quoted
string in the memo or the README may be absent from the file. Tested on a fixture company, with the real
calibration table and citations copied beside its roi config."""

import re
import shutil
from html.parser import HTMLParser
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from roi import calibration, citations
from roi.cli import main

ROOT = Path(__file__).parent.parent
FIXTURES = Path(__file__).parent / "fixtures"
THROUGH = "2026-03"
QUOTED = re.compile(r'"([^"]{12,})"|“([^”]{12,})”')


def roi(work, *args):
    return CliRunner().invoke(main, ["--work-dir", str(work), *args])


class _Text(HTMLParser):
    """The text nodes of a page, outside script and style."""

    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        self._skip += tag in {"script", "style"}

    def handle_endtag(self, tag):
        self._skip -= tag in {"script", "style"} and self._skip > 0

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def page_text(work) -> str:
    p = _Text()
    p.feed((work / "site" / "index.html").read_text(encoding="utf-8"))
    return " ".join(p.parts)


def quoted_strings(text: str) -> list[str]:
    return [a or b for a, b in QUOTED.findall(text)]


def write_citations(work, rows):
    (work / "config" / "citations.yaml").write_text(yaml.safe_dump({"rows": rows}, sort_keys=False), encoding="utf-8")


def read_rows(work) -> list[dict]:
    return yaml.safe_load((work / "config" / "citations.yaml").read_text(encoding="utf-8"))["rows"]


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    work = tmp_path_factory.mktemp("citations")
    (work / "config").mkdir()
    shutil.copy(FIXTURES / "roi_fixture.yaml", work / "config" / "roi.yaml")
    for name in ("calibration.yaml", "citations.yaml"):
        shutil.copy(ROOT / "config" / name, work / "config" / name)
    result = roi(work, "build", "--config", str(FIXTURES / "gen_fixture.yaml"))
    assert result.exit_code == 0, result.output
    assert roi(work, "memo", "--month", THROUGH).exit_code == 0
    return work


@pytest.fixture
def copy(built, tmp_path):
    shutil.copytree(built, tmp_path / "w", symlinks=True)
    return tmp_path / "w"


def test_every_calibration_source_has_a_verified_citation_row():
    cited = citations.load(ROOT / "config" / "citations.yaml")
    for row in calibration.load(ROOT / "config" / "calibration.yaml"):
        c = citations.for_source(cited, row.source_ref)
        assert c is not None, f"{row.team_id}: no citation row for {row.source_ref!r}"
        assert c.url.startswith("https://") and c.retrieved_on and c.verified_on
        assert c in citations.verified(cited)


def test_every_quoted_string_in_the_memo_and_the_readme_is_a_verified_quote(built):
    allowed = {c.quote for c in citations.verified(citations.load(built / "config" / "citations.yaml")) if c.quote}
    assert allowed
    for text in (page_text(built), (ROOT / "README.md").read_text(encoding="utf-8")):
        stray = [q for q in quoted_strings(text) if q not in allowed]
        assert not stray, f"quoted text that is not in config/citations.yaml: {stray}"


def test_the_quoted_string_check_sees_straight_and_curly_quotes():
    text = 'a "short" one, "a quoted string of some length" and “another quoted string”.'
    assert quoted_strings(text) == ["a quoted string of some length", "another quoted string"]


def test_the_memo_prints_each_verified_quote_and_links_its_source(built):
    html = (built / "site" / "index.html").read_text(encoding="utf-8")
    text = page_text(built)
    cited = citations.verified(citations.load(built / "config" / "citations.yaml"))
    assert cited
    for c in cited:
        assert f'<a href="{c.url}">' in html
        assert "<q class=\"lit\">" in html and c.quote in text


def test_a_citation_without_verified_on_renders_no_quote_and_no_link(copy):
    rows = read_rows(copy)
    dropped = rows[0]
    del dropped["verified_on"]
    write_citations(copy, rows)
    assert roi(copy, "memo", "--month", THROUGH).exit_code == 0
    html = (copy / "site" / "index.html").read_text(encoding="utf-8")
    assert dropped["source_ref"] in html
    assert dropped["quote"] not in html and dropped["url"] not in html
    assert rows[1]["quote"] in html and rows[1]["url"] in html


def test_a_source_with_no_citation_row_renders_without_quote_or_link(copy):
    write_citations(copy, read_rows(copy)[1:])
    assert roi(copy, "memo", "--month", THROUGH).exit_code == 0
    html = (copy / "site" / "index.html").read_text(encoding="utf-8")
    assert "Cui, Demirer" in html and "doi.org/10.1287" not in html


def test_without_a_citations_file_the_memo_quotes_nothing(copy):
    (copy / "config" / "citations.yaml").unlink()
    assert roi(copy, "memo", "--month", THROUGH).exit_code == 0
    html = (copy / "site" / "index.html").read_text(encoding="utf-8")
    assert "Brynjolfsson" in html and "<q" not in html and "doi.org" not in html


def test_a_citations_file_that_does_not_load_stops_the_memo(copy):
    write_citations(copy, [{**read_rows(copy)[0], "url": "http://example.org"}])
    result = roi(copy, "memo", "--month", THROUGH)
    assert result.exit_code == 2 and "https://" in result.output


def malformed():
    good = {"key": "k", "source_ref": "S", "url": "https://example.org/p", "retrieved_on": "2026-10-14",
            "verified_on": "2026-10-14", "quote": "a quote of some length"}
    return {
        "unknown key": ({**good, "extra": 1}, "unknown key 'extra'"),
        "missing url": ({k: v for k, v in good.items() if k != "url"}, "missing url"),
        "missing retrieved_on": ({k: v for k, v in good.items() if k != "retrieved_on"}, "missing retrieved_on"),
        "date does not parse": ({**good, "retrieved_on": "2026-13-45"}, "retrieved_on must be an ISO date"),
        "verified_on does not parse": ({**good, "verified_on": "yesterday"}, "verified_on must be an ISO date"),
        "plain http": ({**good, "url": "http://example.org"}, "url must start with https://"),
        "empty quote": ({**good, "quote": " "}, "quote must be a non-empty string"),
    }


@pytest.mark.parametrize("case", sorted(malformed()))
def test_a_malformed_row_fails_load(tmp_path, case):
    row, message = malformed()[case]
    path = tmp_path / "citations.yaml"
    path.write_text(yaml.safe_dump({"rows": [row]}), encoding="utf-8")
    with pytest.raises(ValueError, match=re.escape(message)):
        citations.load(path)


def test_a_well_formed_file_loads_and_an_unknown_top_level_key_does_not(tmp_path):
    path = tmp_path / "citations.yaml"
    path.write_text(yaml.safe_dump({"rows": []}), encoding="utf-8")
    assert citations.load(path) == []
    path.write_text(yaml.safe_dump({"rows": [], "other": 1}), encoding="utf-8")
    with pytest.raises(ValueError, match="unknown top-level key"):
        citations.load(path)
