"""`roi memo`: the static site, its provenance pages and the check that every number resolves
(Scenario 6), tested on a fixture company because the real company's values per unit come later."""

import hashlib
import shutil
from pathlib import Path

import pytest
from click.testing import CliRunner

from conftest import connect
from roi import memo, prov
from roi.cli import main
from roi.workdir import RoiError, WorkDir

FIXTURES = Path(__file__).parent / "fixtures"
THROUGH = "2026-03"


def roi(work, *args):
    return CliRunner().invoke(main, ["--work-dir", str(work), *args])


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    work = tmp_path_factory.mktemp("memo")
    (work / "config").mkdir()
    shutil.copy(FIXTURES / "roi_fixture.yaml", work / "config" / "roi.yaml")
    result = roi(work, "build", "--config", str(FIXTURES / "gen_fixture.yaml"))
    assert result.exit_code == 0, result.output
    assert roi(work, "memo", "--month", THROUGH).exit_code == 0
    return work


@pytest.fixture
def copy(built, tmp_path):
    shutil.copytree(built, tmp_path / "w", symlinks=True)
    return tmp_path / "w"


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


def test_scenario_6_every_number_resolves(built):
    result = roi(built, "memo", "--month", THROUGH, "--check")
    assert result.exit_code == 0, result.output
    assert "memo check:" in result.output and "sign-off: pending" in result.output
    s = scan(built)
    assert s.tagged and not s.untagged
    for tag in s.tagged:
        assert tag["href"] == f"prov/{tag['id']}.html"
        assert (built / "site" / "prov" / f"{tag['id']}.html").exists()


def test_the_three_questions_appear_in_order(built):
    assert scan(built).headings[:3] == list(memo.QUESTIONS)


def test_the_planted_statement_is_above_the_first_question(built):
    html = (built / "site" / "index.html").read_text(encoding="utf-8")
    above = html[: html.index("<h2")]
    assert "effects are planted" in above
    assert "shows that the method works" in above and "not a finding about any real company" in above


def test_the_check_reports_sections_and_lists_the_static_numbers(built):
    out = roi(built, "memo", "--month", THROUGH, "--check").output
    for section in ("headline", "return", "allocation", "agenda", "assumptions"):
        assert f"  {section}: " in out
    assert "static numbers for review: 95%" in out


def test_a_measured_teams_figures_are_the_warehouse_figures(built):
    con = connect(str(built / "warehouse" / "roi.duckdb"), read_only=True)
    effect, lo, hi = con.execute(
        "SELECT effect, ci_lo, ci_hi FROM est_team_effect WHERE team_id = 'support' AND through_month = ?", [THROUGH]
    ).fetchone()
    multiple = con.execute(
        "SELECT roi_multiple FROM roi_team WHERE team_id = 'support' AND through_month = ?", [THROUGH]
    ).fetchone()[0]
    _, collected = memo.collect(WorkDir(built), THROUGH, con)
    shown = {r.name: r.display_value for r in collected.records.values()}
    assert shown["effect:support"] == f"{effect:.3f}"
    assert shown["effect_lo:support"] == f"{lo:.3f}" and shown["effect_hi:support"] == f"{hi:.3f}"
    assert shown["roi:support"] == f"{multiple:.2f}"


def test_unmeasured_and_control_teams_get_a_reason_and_no_figure(built):
    html = (built / "site" / "index.html").read_text(encoding="utf-8")
    assert "<code>no_pre_period</code>" in html and "<code>too_recent</code>" in html
    assert "control. It has had no AI tools" in html
    con = connect(str(built / "warehouse" / "roi.duckdb"), read_only=True)
    _, collected = memo.collect(WorkDir(built), THROUGH, con)
    for team in ("platform_engineering", "legal_operations", "people_operations"):
        names = {r.name.split(":")[0] for r in collected.records.values() if r.name.endswith(f":{team}")}
        assert names <= {"payroll_share", "agenda_n_per_arm", "agenda_length_months", "agenda_delta", "allocation",
                         "funded_share"}, (team, names)
        assert not names & {"effect", "roi", "spend", "benefit"}


def test_a_provenance_page_states_the_record_and_its_rows_hash_to_the_checksum(built):
    s = scan(built)
    pages = [memo._ProvPage() for _ in s.tagged]
    for tag, page in zip(s.tagged, pages):
        page.feed((built / "site" / "prov" / f"{tag['id']}.html").read_text(encoding="utf-8"))
        assert page.attrs["data-prov-page"] == tag["id"]
        assert prov.checksum([tuple(r) for r in page.rows]) == page.attrs["data-checksum"]
        assert len(page.rows) == int(page.attrs["data-rows"])


def test_a_page_shows_at_most_the_row_cap_and_states_the_full_count(built, monkeypatch, tmp_path):
    monkeypatch.setattr(memo, "ROW_CAP", 2)
    shutil.copytree(built, tmp_path / "w", symlinks=True)
    work = tmp_path / "w"
    assert roi(work, "memo", "--month", THROUGH, "--check").exit_code == 0
    headline = scan(work).tagged[0]["id"]
    page = memo._ProvPage()
    page.feed((work / "site" / "prov" / f"{headline}.html").read_text(encoding="utf-8"))
    assert len(page.rows) == 2 and page.attrs["data-rows"] != "2"
    assert "Only the first rows are shown" in (work / "site" / "prov" / f"{headline}.html").read_text(encoding="utf-8")


def test_the_site_is_byte_identical_across_rebuilds(built, copy):
    assert roi(copy, "memo", "--month", THROUGH).exit_code == 0
    for rel in sorted(p.relative_to(built) for p in (built / "site").rglob("*") if p.is_file()):
        assert (built / rel).read_bytes() == (copy / rel).read_bytes(), rel
    assert {p.name for p in (built / "site" / "prov").iterdir()} == {p.name for p in (copy / "site" / "prov").iterdir()}


def test_the_check_does_not_change_the_warehouse(built):
    db = built / "warehouse" / "roi.duckdb"
    before = hashlib.sha256(db.read_bytes()).hexdigest()
    assert roi(built, "memo", "--month", THROUGH, "--check").exit_code == 0
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before


def test_the_degenerate_allocation_banner_appears_when_nothing_is_funded(built):
    html = (built / "site" / "index.html").read_text(encoding="utf-8")
    assert 'id="degenerate"' in html and "Allocation degenerate" in html


# ---- the ways the check fails ----------------------------------------------------------------------

def test_a_result_row_changed_after_the_site_was_built_fails(copy):
    edit(copy, "UPDATE est_team_effect SET effect = effect + 1e-9 WHERE team_id = 'support'")
    con = memo.open_warehouse(WorkDir(copy))
    with pytest.raises(RoiError, match="checksum mismatch for"):
        memo.check_site(WorkDir(copy), THROUGH, con)


def test_a_stored_record_catches_a_mart_row_changed_before_the_site_is_rebuilt(copy):
    edit(copy, "UPDATE fct_team_month SET spend_cents = spend_cents + 1 WHERE team_id = 'support' AND month = DATE '2026-03-01'")
    result = roi(copy, "memo", "--month", THROUGH, "--check")
    assert result.exit_code == 1 and "checksum mismatch" in result.output


def test_a_number_whose_record_was_deleted_stops_the_memo(copy):
    edit(copy, "DELETE FROM prov_record WHERE name = 'effect:support'")
    for args in (("memo", "--month", THROUGH), ("memo", "--month", THROUGH, "--check")):
        result = roi(copy, *args)
        assert result.exit_code == 1, result.output
        assert "no provenance record for effect:support at 2026-03; rebuild the warehouse" in result.output


def test_a_hand_edited_number_fails(copy):
    page = copy / "site" / "index.html"
    html = page.read_text(encoding="utf-8")
    tag = scan(copy).tagged[3]
    page.write_text(html.replace(f'data-prov="{tag["id"]}">{tag["text"]}<', f'data-prov="{tag["id"]}">9{tag["text"]}<', 1))
    con = memo.open_warehouse(WorkDir(copy))
    with pytest.raises(RoiError, match="records"):
        memo.check_site(WorkDir(copy), THROUGH, con)


def test_a_number_outside_a_tag_fails(copy):
    page = copy / "site" / "index.html"
    page.write_text(page.read_text(encoding="utf-8").replace("</ul>", "</ul><p>We saved 40 hours.</p>"))
    con = memo.open_warehouse(WorkDir(copy))
    with pytest.raises(RoiError, match="untagged number in memo body: 40"):
        memo.check_site(WorkDir(copy), THROUGH, con)


def test_a_number_that_does_not_link_to_its_page_fails(copy):
    page = copy / "site" / "index.html"
    tag = scan(copy).tagged[0]
    page.write_text(page.read_text(encoding="utf-8").replace(f'href="prov/{tag["id"]}.html"', 'href="elsewhere.html"', 1))
    con = memo.open_warehouse(WorkDir(copy))
    with pytest.raises(RoiError, match="does not link to its provenance page"):
        memo.check_site(WorkDir(copy), THROUGH, con)


def test_a_provenance_page_that_disagrees_with_its_record_fails(copy):
    tag = scan(copy).tagged[0]
    page = copy / "site" / "prov" / f"{tag['id']}.html"
    html = page.read_text(encoding="utf-8")
    page.write_text(html.replace('data-rows="', 'data-rows="9', 1))
    con = memo.open_warehouse(WorkDir(copy))
    with pytest.raises(RoiError, match="disagrees with its record"):
        memo.check_site(WorkDir(copy), THROUGH, con)


def test_rows_on_a_page_that_do_not_hash_to_the_checksum_fail(copy):
    tag = scan(copy).tagged[0]
    page = copy / "site" / "prov" / f"{tag['id']}.html"
    html = page.read_text(encoding="utf-8")
    page.write_text(html.replace("<tbody>\n<tr><td>", "<tbody>\n<tr><td>x", 1))
    con = memo.open_warehouse(WorkDir(copy))
    with pytest.raises(RoiError, match="do not hash to its checksum"):
        memo.check_site(WorkDir(copy), THROUGH, con)


def test_a_changed_assumption_in_the_config_needs_a_rebuild_of_the_warehouse(copy):
    config = copy / "config" / "roi.yaml"
    page = copy / "site" / "index.html"
    html = page.read_text(encoding="utf-8")
    config.write_text(config.read_text().replace("base: 12", "base: 13"))
    result = roi(copy, "memo", "--month", THROUGH, "--check")  # the build stored the old value
    assert result.exit_code == 1 and "rebuild the warehouse" in result.output
    page.write_text(html)
    con = memo.open_warehouse(WorkDir(copy))
    with pytest.raises(RoiError, match="no provenance record|records"):
        memo.check_site(WorkDir(copy), THROUGH, con)


def test_a_one_cent_ledger_change_makes_the_memo_refuse_with_exit_3(copy):
    edit(copy, "UPDATE raw_gl_journal SET debit_cents = debit_cents + 1 WHERE entry_id = (SELECT min(entry_id) FROM raw_gl_journal"
               " WHERE account_code = (SELECT value FROM roi_settings WHERE key = 'gl_account') AND debit_cents > 0)")
    for args in (("memo", "--month", THROUGH), ("memo", "--month", THROUGH, "--check")):
        result = roi(copy, *args)
        assert result.exit_code == 3, result.output
        assert "GL tie failed" in result.output or "disagrees with the books" in result.output or "team=" in result.output


def test_require_signoff_fails_while_the_signoff_is_pending(built):
    result = roi(built, "memo", "--month", THROUGH, "--require-signoff")
    assert result.exit_code == 1
    assert "no sign-off for 2026-03" in result.output


# ---- the number rule (a DOM walk applying the pattern) --------------------------------------------------

def walk(body: str) -> memo._MemoScan:
    s = memo._MemoScan()
    s.feed(f"<html><body>{body}</body></html>")
    return s


def test_dates_and_percentages_outside_a_tag_are_untagged_numbers():
    assert walk("<p>Rolled out in 2025-07.</p>").untagged == ["2025", "-07"]
    assert walk("<p>About 95% sure.</p>").untagged == ["95%"]
    assert walk("<p>Costs $1,200.50 a month.</p>").untagged == ["$1,200.50"]


def test_time_lit_and_tagged_numbers_are_exempt():
    s = walk('<p><time datetime="2025-07">2025-07</time> <span class="lit">95%</span> '
             '<a href="prov/ab.html"><span data-prov="ab">$3.00</span></a></p>')
    assert s.untagged == [] and s.lit == ["95%"]
    assert s.tagged == [{"id": "ab", "href": "prov/ab.html", "text": "$3.00"}]


def test_numbers_in_the_head_are_not_in_the_body():
    s = memo._MemoScan()
    s.feed("<html><head><title>Memo 7</title><style>a{margin:1px}</style></head><body><p>none</p></body></html>")
    assert s.untagged == []


# ---- the calibration paragraph ---------------------------------------------------------------------------

def write_calibration(work, rows):
    import yaml

    (work / "config" / "calibration.yaml").write_text(yaml.safe_dump({"rows": rows}), encoding="utf-8")


def calibration_row(team, source, extrapolated):
    return {"team_id": team, "effect_low": 0.05, "effect_high": 0.2, "source_ref": source,
            "figure": "a figure that stays out of the memo", "verified_on": "2000-01-01", "extrapolated": extrapolated}


def test_the_memo_names_the_teams_calibrated_from_studies_of_different_work(copy):
    write_calibration(copy, [
        calibration_row("sales_development", "Fixture C, 2000, Journal of Invented Results", True),
        calibration_row("support", "Fixture B, 2001, Journal of Invented Results", False),
    ])
    assert roi(copy, "memo", "--month", THROUGH).exit_code == 0
    html = (copy / "site" / "index.html").read_text(encoding="utf-8")
    different = html[html.index('id="calibration-different"'):html.index("</ul>", html.index('id="calibration-different"'))]
    assert "Sales Development" in different and "Fixture C, 2000, Journal of Invented Results" in different
    assert "measured different work" in different
    assert "Support" not in different and "Fixture B" not in different
    same = html[html.index('id="calibration-same"'):html.index("</ul>", html.index('id="calibration-same"'))]
    assert "Support" in same and "Fixture B, 2001, Journal of Invented Results" in same
    assert "different work" not in same
    assert "a figure that stays out of the memo" not in html
    result = roi(copy, "memo", "--month", THROUGH, "--check")
    assert result.exit_code == 0, result.output


def test_without_a_calibration_table_the_paragraph_is_absent(built, copy):
    write_calibration(copy, [])
    assert roi(copy, "memo", "--month", THROUGH).exit_code == 0
    for work in (built, copy):
        html = (work / "site" / "index.html").read_text(encoding="utf-8")
        assert "calibrated" not in html and 'id="calibration' not in html


def test_a_calibration_table_that_does_not_load_stops_the_memo(copy):
    write_calibration(copy, [{**calibration_row("support", "Fixture B", False), "verified_on": None}])
    result = roi(copy, "memo", "--month", THROUGH)
    assert result.exit_code == 2 and "verified_on" in result.output


# ---- the toy company --------------------------------------------------------------------------------------

def test_the_toy_company_memo_resolves_without_a_value_per_unit(tmp_path):
    assert roi(tmp_path, "build", "--toy").exit_code == 0
    result = roi(tmp_path, "memo", "--month", "2025-06", "--check")
    assert result.exit_code == 0, result.output
    assert "headline: 1 number resolved" in result.output
    html = (tmp_path / "site" / "index.html").read_text(encoding="utf-8")
    assert "no value per unit entered" in html
    assert "No allocation was written" in html
