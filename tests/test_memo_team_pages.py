"""`roi memo`: one page per team, built from the index's own numbers and checked with it."""

import re
import shutil
from pathlib import Path

import pytest
from click.testing import CliRunner

from conftest import connect
from roi import memo, signoff
from roi.cli import main
from roi.workdir import RoiError, WorkDir

MONTH = "2025-06"


def roi(work, *args):
    return CliRunner().invoke(main, ["--work-dir", str(work), *args])


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    work = tmp_path_factory.mktemp("team_pages")
    assert roi(work, "build", "--toy").exit_code == 0
    result = roi(work, "memo", "--month", MONTH)
    assert result.exit_code == 0, result.output
    return work


@pytest.fixture
def copy(built, tmp_path):
    shutil.copytree(built, tmp_path / "w", symlinks=True)
    return tmp_path / "w"


def team_ids(work) -> list[str]:
    con = connect(str(work / "warehouse" / "roi.duckdb"))
    try:
        return sorted(t for (t,) in con.execute("SELECT team_id FROM dim_team").fetchall())
    finally:
        con.close()


def scan_page(path: Path) -> memo._MemoScan:
    s = memo._MemoScan()
    s.feed(path.read_text(encoding="utf-8"))
    return s


def test_there_is_one_page_per_team(built):
    pages = sorted(p.stem for p in (built / "site" / "teams").glob("*.html"))
    assert pages == team_ids(built) and len(pages) > 1


def test_every_number_on_a_team_page_is_tagged_and_its_anchor_target_exists(built):
    tagged = 0
    for team in team_ids(built):
        s = scan_page(built / "site" / "teams" / f"{team}.html")
        assert not s.untagged, team
        for tag in s.tagged:
            assert tag["href"] == f"../prov/{tag['id']}.html"
            assert (built / "site" / "prov" / f"{tag['id']}.html").exists()
        tagged += len(s.tagged)
    assert tagged


def test_a_team_page_shows_the_numbers_of_the_index_with_the_same_ids(built):
    index_ids = {t["id"] for t in scan_page(built / "site" / "index.html").tagged}
    for team in team_ids(built):
        assert {t["id"] for t in scan_page(built / "site" / "teams" / f"{team}.html").tagged} <= index_ids


def test_a_team_page_states_the_team_its_metric_and_its_status(built):
    html = (built / "site" / "teams" / f"{team_ids(built)[0]}.html").read_text(encoding="utf-8")
    assert re.search(r"<h1>[^<]+</h1>", html)
    assert "is better" in html and "Status: " in html
    assert 'href="../index.html"' in html


def test_the_planted_banner_is_on_every_page(built):
    for team in team_ids(built):
        html = (built / "site" / "teams" / f"{team}.html").read_text(encoding="utf-8")
        assert memo.PLANTED_STATEMENT in html


def test_the_index_links_every_team_page(built):
    html = (built / "site" / "index.html").read_text(encoding="utf-8")
    for team in team_ids(built):
        assert f'<a href="teams/{team}.html">' in html


def test_the_check_counts_the_pages(built):
    result = roi(built, "memo", "--month", MONTH, "--check")
    assert result.exit_code == 0, result.output
    assert f"pages: {1 + len(team_ids(built))} checked" in result.output


def test_a_hand_edited_number_on_a_team_page_fails_the_check_with_exit_1(copy):
    team = team_ids(copy)[0]
    path = copy / "site" / "teams" / f"{team}.html"
    tag = scan_page(path).tagged[0]
    html = path.read_text(encoding="utf-8")
    path.write_text(html.replace(f'data-prov="{tag["id"]}">{tag["text"]}<', f'data-prov="{tag["id"]}">9{tag["text"]}<', 1))
    con = memo.open_warehouse(WorkDir(copy))
    with pytest.raises(RoiError, match=f"teams/{team}.html: .*records") as e:
        memo.check_site(WorkDir(copy), MONTH, con)
    assert e.value.exit_code == 1


def test_a_number_outside_a_tag_on_a_team_page_fails(copy):
    team = team_ids(copy)[0]
    path = copy / "site" / "teams" / f"{team}.html"
    path.write_text(path.read_text(encoding="utf-8").replace("</body>", "<p>We saved 40 hours.</p></body>"))
    con = memo.open_warehouse(WorkDir(copy))
    with pytest.raises(RoiError, match="untagged number in memo body: 40"):
        memo.check_site(WorkDir(copy), MONTH, con)


def test_a_missing_team_page_fails_the_check(copy):
    team = team_ids(copy)[0]
    (copy / "site" / "teams" / f"{team}.html").unlink()
    con = memo.open_warehouse(WorkDir(copy))
    with pytest.raises(RoiError, match=f"teams/{team}.html is missing"):
        memo.check_site(WorkDir(copy), MONTH, con)


def test_a_team_page_without_the_planted_banner_fails(copy):
    team = team_ids(copy)[0]
    path = copy / "site" / "teams" / f"{team}.html"
    path.write_text(path.read_text(encoding="utf-8").replace(memo.PLANTED_STATEMENT, ""))
    con = memo.open_warehouse(WorkDir(copy))
    with pytest.raises(RoiError, match="planted-effects statement is missing"):
        memo.check_site(WorkDir(copy), MONTH, con)


def test_a_rebuild_removes_a_stale_team_page(copy):
    stale = copy / "site" / "teams" / "gone.html"
    stale.write_text("<html></html>")
    assert roi(copy, "memo", "--month", MONTH).exit_code == 0
    assert not stale.exists()


def test_the_site_is_byte_identical_across_rebuilds(built, copy):
    assert roi(copy, "memo", "--month", MONTH).exit_code == 0
    for team in team_ids(built):
        name = f"teams/{team}.html"
        assert (copy / "site" / name).read_bytes() == (built / "site" / name).read_bytes()


def test_the_signoff_memo_hash_changes_when_a_team_page_changes(copy):
    site = WorkDir(copy).site
    before = signoff.memo_hash(site)
    path = site / "teams" / f"{team_ids(copy)[0]}.html"
    path.write_text(path.read_text(encoding="utf-8") + "<!-- edited -->")
    assert signoff.memo_hash(site) != before
