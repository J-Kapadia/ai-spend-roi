"""Render the static memo site and check that every number in it resolves.

The memo reads only the result tables and the named marts. Every number in it is tagged
`data-prov="<id>"`, wrapped in an anchor to `site/prov/<id>.html`, and backed by a provenance
record: the `prov_record` row the build stored (a named table, a predicate bounded at the
month, a row count and a checksum). The memo never derives a record, so a number whose record the
build did not write stops it. `roi memo --check` re-runs every predicate and compares.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path

import duckdb
import jinja2

from roi.db import connect as db_connect
from roi import benchmark, calibration, citations, prov, verify_books
from roi.numbers import SPEND_NAME, Number, by_name, enumerate_numbers, has_table
from roi import roi as roi_calc
from roi.prov import Record
from roi.steps import roi as roi_step
from roi.workdir import RoiError, WorkDir

# A "number" in the memo body.
NUMBER = re.compile(r"[$-]?\d[\d,]*(\.\d+)?%?")
ROW_CAP = 200
VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}

_env = jinja2.Environment(
    loader=jinja2.PackageLoader("roi", "templates"), autoescape=True, keep_trailing_newline=True,
    trim_blocks=True, lstrip_blocks=True,
)

REASON_TEXT = {
    "no_pre_period": "AI tools were in place from the first month of the books, so there is no period before them to compare with.",
    "too_recent": "AI tools arrived too recently for enough months to have passed since the rollout.",
    "no_comparison": "every team had AI tools by the time of the comparison, so no team is left to compare with.",
    "pre_trend": "the team was already drifting away from its comparison teams before the rollout, so the comparison would not be fair.",
    "variance_not_computable": "the comparison is too thin to put an interval around the effect.",
}
DESIGN_TEXT = {
    "holdback": "Hold AI tools back from a random share of the team for the length of the test.",
    "wait": "Wait for more months after the rollout, then estimate again.",
    "pair_stagger": "Pair this team with another and stagger their start dates.",
    "not_testable_alone": "Not testable alone: pair it with another team on a staggered start.",
}
VERDICT_TEXT = {
    "good": "pays off at the stated value",
    "poor": "does not pay off at the stated value",
    "not_distinguishable": "cannot be told apart from break-even",
}


@dataclass(frozen=True)
class Num:
    """A number to print: its text and the provenance record behind it."""

    prov_id: str
    text: str


@dataclass
class Collector:
    """Looks up the stored provenance records of the numbers a memo prints, one section at a time."""

    con: duckdb.DuckDBPyConnection
    month: str
    config: Path | None
    section: str = ""
    records: dict[str, Record] = field(default_factory=dict)
    section_of: dict[str, str] = field(default_factory=dict)

    def _keep(self, rec: Record) -> Num:
        self.records[rec.prov_id] = rec
        self.section_of.setdefault(rec.prov_id, self.section)
        return Num(rec.prov_id, rec.display_value)

    def stored(self, rec: Record) -> Num:
        return self._keep(rec)

    def number(self, n: Number) -> Num:
        self.section = n.section
        rec = prov.fetch(self.con, prov.make_id(n.name, self.month, n.source_model, n.source_predicate, n.display_value))
        if rec is None:
            raise RoiError(1, f"no provenance record for {n.name} at {self.month}; rebuild the warehouse")
        return self._keep(rec)


def calibration_path(roi_config: Path) -> Path:
    """The calibration table that goes with a roi config: `roi.yaml` -> `calibration.yaml`, `roi_x.yaml` -> `calibration_x.yaml`."""
    stem = roi_config.stem
    suffix = stem[3:] if stem == "roi" or stem.startswith("roi_") else ""
    return roi_config.with_name("calibration" + suffix + roi_config.suffix)


def citations_path(roi_config: Path) -> Path:
    """The citations that go with a roi config: beside it, as `citations.yaml`."""
    return roi_config.with_name("citations" + roi_config.suffix)


def citation_rows(work: WorkDir) -> list[citations.Citation]:
    """The verified citation rows; none when the file is absent, so no source is quoted or linked."""
    path = citations_path(roi_step.config_path(work))
    if not path.exists():
        return []
    try:
        return citations.verified(citations.load(path))
    except ValueError as e:
        raise RoiError(2, str(e)) from e


def calibration_rows(work: WorkDir, teams: list[dict]) -> list[dict]:
    """The calibration rows of the teams in the memo, by team name; none when the table is missing or empty.

    A row carries `url` and `quote` only when its source has a citation row with `verified_on`."""
    path = calibration_path(roi_step.config_path(work))
    if not path.exists():
        return []
    try:
        rows = calibration.load(path)
    except ValueError as e:
        raise RoiError(2, str(e)) from e
    cited = citation_rows(work)
    names = {t["team"]: t["name"] for t in teams}
    out = []
    for r in rows:
        if r.team_id not in names:
            continue
        c = citations.for_source(cited, r.source_ref)
        out.append({"name": names[r.team_id], "source": r.source_ref, "extrapolated": r.extrapolated,
                    "url": c.url if c else None, "quote": c.quote if c else None})
    return sorted(out, key=lambda r: r["name"])


def open_warehouse(work: WorkDir) -> duckdb.DuckDBPyConnection:
    if not work.warehouse_db.exists():
        raise RoiError(2, f"no warehouse at {work.warehouse_db}; run `roi build` first")
    return db_connect(work.warehouse_db, read_only=True)


def roi_config_path(work: WorkDir) -> Path | None:
    path = roi_step.config_path(work)
    return path if path.exists() else None


def collect(work: WorkDir, month: str, con: duckdb.DuckDBPyConnection) -> tuple[dict, Collector]:
    """The template context for `month`, and the provenance records of every number in it."""
    head = prov.fetch_named(con, SPEND_NAME, month)
    if head is None:
        raise RoiError(2, f"no results for month {month}")
    config = roi_config_path(work)
    c = Collector(con, month, config)
    numbers = by_name(enumerate_numbers(con, month, config))
    at = f"through_month = '{month}'"

    def num(name: str) -> Num | None:
        n = numbers.get(name)
        return None if n is None else c.number(n)

    # The headline is looked up by name: its text is the stored record's, whatever the mart says now.
    c.section = "headline"
    headline = c.stored(head)

    roi_verdicts = {}
    if has_table(con, "roi_team"):
        roi_verdicts = dict(con.execute(f"SELECT team_id, roi_verdict FROM roi_team WHERE {at}").fetchall())

    teams = []
    for team, name, status, reason, unit, kind in con.execute(
        "SELECT t.team_id, t.name, e.status, e.reason, t.unit_label, t.kind"
        f" FROM est_team_effect e JOIN dim_team t USING (team_id) WHERE e.{at} ORDER BY t.name"
    ).fetchall():
        row = {"team": team, "name": name, "status": status, "reason": reason, "unit": unit, "kind": kind,
               "share": num(f"payroll_share:{team}"), "measured": status == "measured", "roi": None}
        if status == "measured":
            row["effect"], row["lo"], row["hi"] = num(f"effect:{team}"), num(f"effect_lo:{team}"), num(f"effect_hi:{team}")
            if team in roi_verdicts:
                verdict = roi_verdicts[team]
                row["roi"] = {
                    "spend": num(f"spend:{team}"),
                    "benefit": num(f"benefit:{team}"),
                    "multiple": num(f"roi:{team}"),
                    "lo": num(f"roi_lo:{team}"),
                    "hi": num(f"roi_hi:{team}"),
                    "verdict": VERDICT_TEXT.get(verdict, verdict),
                }
        else:
            row["why"] = REASON_TEXT.get(reason, "no comparison group is available") if status == "unmeasured" else None
        teams.append(row)
    company_share = num("payroll_share:company")

    sensitivity = {"multipliers": [], "rows": []}
    if has_table(con, "roi_sensitivity") and roi_verdicts:
        grid = con.execute(
            f"SELECT team_id, value_multiplier FROM roi_sensitivity WHERE {at} ORDER BY team_id, value_multiplier"
        ).fetchall()
        multipliers = sorted({m for _, m in grid})
        names = {t["team"]: t["name"] for t in teams}
        sensitivity["multipliers"] = [num(f"multiplier:{m!r}") for m in multipliers]
        for team in sorted({team for team, _ in grid}, key=lambda t: names.get(t, t)):
            sensitivity["rows"].append({
                "name": names.get(team, team),
                "cells": [num(f"sensitivity:{team}:{m!r}") for m in multipliers],
                "survives": num(f"survives:{team}"),
            })

    allocation = {"budget": None, "rows": [], "degenerate": False}
    if has_table(con, "alloc_result"):
        identified = {}
        if has_table(con, "fit_curve"):
            identified = dict(con.execute(f"SELECT team_id, identified FROM fit_curve WHERE {at}").fetchall())
        rows = con.execute(
            "SELECT a.team_id, t.name, a.amount_cents, a.capped"
            f" FROM alloc_result a JOIN dim_team t USING (team_id) WHERE a.{at} ORDER BY t.name"
        ).fetchall()
        if rows:
            allocation["budget"] = num("allocation_budget")
            for team, name, _, capped in rows:
                allocation["rows"].append({
                    "name": name, "capped": capped, "identified": identified.get(team),
                    "amount": num(f"allocation:{team}"), "funded": num(f"funded_share:{team}"),
                })
            funded_rows = [r for r in rows if r[2] > 0]
            allocation["degenerate"] = len(funded_rows) < 2 or all(r[3] for r in funded_rows)

    agenda = {"rows": [], "missing": []}
    items = {}
    if has_table(con, "agenda_item"):
        items = {r[0]: r for r in con.execute(
            f"SELECT team_id, design FROM agenda_item WHERE {at}"
        ).fetchall()}
    for t in teams:
        if t["status"] != "unmeasured":
            continue
        if t["team"] not in items:
            agenda["missing"].append(t["name"])
            continue
        design = items[t["team"]][1]
        agenda["rows"].append({
            "name": t["name"], "reason": t["reason"], "design": DESIGN_TEXT.get(design, design),
            "n": num(f"agenda_n_per_arm:{t['team']}"),
            "length": num(f"agenda_length_months:{t['team']}"),
            "delta": num(f"agenda_delta:{t['team']}"),
        })

    assumptions = []
    for t in teams:
        if not t["measured"] or f"value_low:{t['team']}" not in numbers:
            continue
        low, base, high = (num(f"value_{part}:{t['team']}") for part in ("low", "base", "high"))
        assumptions.append({"name": t["name"], "unit": t["unit"], "kind": t["kind"], "low": low, "base": base, "high": high})

    c.section = benchmark.SECTION
    benchmark_nums = {n.name: c.number(n) for n in numbers.values() if n.section == benchmark.SECTION}

    context = {
        "month": month, "headline": headline, "teams": teams, "company_share": company_share,
        "sensitivity": sensitivity, "allocation": allocation, "agenda": agenda, "assumptions": assumptions,
        "unmeasured": [t for t in teams if t["status"] != "measured"], "calibration": calibration_rows(work, teams),
        "benchmark": benchmark.context(con, month, benchmark_nums),
    }
    return context, c


def build_site(work: WorkDir, month: str, con: duckdb.DuckDBPyConnection) -> Path:
    verify_books.check_gl_tie(con, month)
    context, c = collect(work, month, con)

    prov_dir = work.site / "prov"
    prov_dir.mkdir(parents=True, exist_ok=True)
    for stale in prov_dir.glob("*.html"):
        stale.unlink()
    page = _env.get_template("prov.html.j2")
    for rec in c.records.values():
        columns, rows = prov.rows_of(con, rec.source_model, rec.source_predicate, c.config)
        (prov_dir / f"{rec.prov_id}.html").write_text(
            page.render(rec=rec, month=month, columns=columns,
                        rows=[[prov._fmt(v) for v in r] for r in rows[:ROW_CAP]], capped=len(rows) > ROW_CAP),
            encoding="utf-8",
        )
    index = work.site / "index.html"
    index.write_text(_env.get_template("memo.html.j2").render(**context), encoding="utf-8")
    return index


class _MemoScan(HTMLParser):
    """DOM walk over the body: collects tagged numbers and numbers outside any tag."""

    def __init__(self) -> None:
        super().__init__()
        self.stack: list[tuple[str, dict[str, str | None]]] = []
        self.tagged: list[dict] = []
        self.untagged: list[str] = []
        self.lit: list[str] = []
        self.headings: list[str] = []
        self._heading: str | None = None

    def handle_starttag(self, tag, attrs):
        if tag in VOID_TAGS:
            return
        a = dict(attrs)
        if "data-prov" in a:
            anchor = next((p[1] for p in reversed(self.stack) if p[0] == "a"), {})
            self.tagged.append({"id": a["data-prov"], "href": anchor.get("href"), "text": ""})
        if tag == "h2":
            self._heading = ""
        self.stack.append((tag, a))

    def handle_endtag(self, tag):
        if tag == "h2" and self._heading is not None:
            self.headings.append(self._heading.strip())
            self._heading = None
        while self.stack:
            if self.stack.pop()[0] == tag:
                break

    def _is_lit(self) -> bool:
        return any("lit" in (a.get("class") or "").split() for _, a in self.stack)

    def _exempt(self) -> bool:
        for tag, a in self.stack:
            if tag in {"time", "script", "style"} or "data-prov" in a:
                return True
        return self._is_lit()

    def handle_data(self, data):
        if not any(tag == "body" for tag, _ in self.stack):
            return
        if self._heading is not None:
            self._heading += data
        in_tag = next((t for t in reversed(self.tagged) if any("data-prov" in a for _, a in self.stack)), None)
        if in_tag is not None:
            in_tag["text"] += data
        if self._is_lit():
            self.lit += [m.group(0) for m in NUMBER.finditer(data)]
        if not self._exempt():
            self.untagged += [m.group(0) for m in NUMBER.finditer(data)]


class _ProvPage(HTMLParser):
    """The stated fields of a provenance page, and the rows it shows."""

    def __init__(self) -> None:
        super().__init__()
        self.attrs: dict[str, str | None] = {}
        self.rows: list[list[str]] = []
        self._in_body = False
        self._cell: str | None = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if "data-prov-page" in a:
            self.attrs = a
        if tag == "tbody":
            self._in_body = True
        elif self._in_body and tag == "tr":
            self.rows.append([])
        elif self._in_body and tag == "td":
            self._cell = ""

    def handle_endtag(self, tag):
        if tag == "tbody":
            self._in_body = False
        elif tag == "td" and self._cell is not None:
            self.rows[-1].append(self._cell)
            self._cell = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell += data


@dataclass(frozen=True)
class Checked:
    ids: list[str]  # one per tagged number, in page order
    by_section: dict[str, int]
    lit: list[str]  # the static numbers inside `.lit`, listed for review


QUESTIONS = ("Does our AI spend pay off?", "Where does the next dollar go?", "What should we test next?")


def check_site(work: WorkDir, month: str, con: duckdb.DuckDBPyConnection) -> Checked:
    """Re-run every provenance predicate; raise RoiError(1, ...) on the first failure."""
    verify_books.check_gl_tie(con, month)
    scan = _MemoScan()
    scan.feed((work.site / "index.html").read_text(encoding="utf-8"))
    if scan.untagged:
        raise RoiError(1, f"untagged number in memo body: {scan.untagged[0]}")
    if not scan.tagged:
        raise RoiError(1, "memo has no tagged numbers")
    if [h for h in scan.headings if h in QUESTIONS] != list(QUESTIONS):
        raise RoiError(1, "the three questions do not appear in order")
    _, collected = collect(work, month, con)
    for tag in scan.tagged:
        rec = collected.records.get(tag["id"]) or prov.fetch(con, tag["id"])
        if rec is None:
            raise RoiError(1, f"number {tag['text']!r} has no provenance record {tag['id']}")
        rows, digest = prov.recheck(con, rec, collected.config)
        if (rows, digest) != (rec.source_rows, rec.source_checksum):
            raise RoiError(1, f"checksum mismatch for {rec.prov_id}: stored {rec.source_rows} rows, now {rows}")
        if tag["text"] != rec.display_value:
            raise RoiError(1, f"memo shows {tag['text']!r} but {rec.prov_id} records {rec.display_value!r}")
        page_path = work.site / "prov" / f"{rec.prov_id}.html"
        if tag["href"] != f"prov/{rec.prov_id}.html" or not page_path.exists():
            raise RoiError(1, f"number {rec.prov_id} does not link to its provenance page")
        page = _ProvPage()
        page.feed(page_path.read_text(encoding="utf-8"))
        if page.attrs.get("data-rows") != str(rec.source_rows) or page.attrs.get("data-checksum") != rec.source_checksum:
            raise RoiError(1, f"provenance page for {rec.prov_id} disagrees with its record")
        if rec.source_rows < ROW_CAP and prov.checksum([tuple(r) for r in page.rows]) != rec.source_checksum:
            raise RoiError(1, f"the rows shown on the page of {rec.prov_id} do not hash to its checksum")
    by_section: dict[str, int] = {}
    for tag in scan.tagged:
        section = collected.section_of.get(tag["id"], "other")
        by_section[section] = by_section.get(section, 0) + 1
    return Checked([t["id"] for t in scan.tagged], by_section, scan.lit)
