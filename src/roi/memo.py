"""Render the static memo site and check that every number in it resolves.

The memo reads only the result tables and the named marts. Every number in it is tagged
`data-prov="<id>"`, wrapped in an anchor to `site/prov/<id>.html`, and backed by a provenance
record: the stored `prov_record` row for the headline figure, and a record derived from the
same warehouse for each figure of the result tables (a named table, a predicate bounded at the
month, a row count and a checksum). `roi memo --check` re-runs every predicate and compares.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path

import duckdb
import jinja2

from roi.db import connect as db_connect
from roi import calibration, prov, verify_books
from roi import roi as roi_calc
from roi.prov import Record
from roi.steps import roi as roi_step
from roi.steps.provenance import SPEND_NAME
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
    """Builds the provenance records of the numbers a memo prints, one section at a time."""

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

    def num(self, name: str, model: str, predicate: str, display: str, value: float) -> Num:
        return self._keep(prov.derive(self.con, name, self.month, model, predicate, display, value, self.config))


def usd(cents: int) -> str:
    return f"{'-' if cents < 0 else ''}${abs(cents) // 100:,}.{abs(cents) % 100:02d}"


def usd_unit(v: float) -> str:
    return f"${v:,.2f}" if v >= 1 else f"${v:.4f}"


def _q(value: str) -> str:
    return value.replace("'", "''")


def _has(con: duckdb.DuckDBPyConnection, table: str) -> bool:
    return bool(con.execute(
        "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'main' AND table_name = ?", [table]
    ).fetchone()[0])


def calibration_path(roi_config: Path) -> Path:
    """The calibration table that goes with a roi config: `roi.yaml` -> `calibration.yaml`, `roi_x.yaml` -> `calibration_x.yaml`."""
    stem = roi_config.stem
    suffix = stem[3:] if stem == "roi" or stem.startswith("roi_") else ""
    return roi_config.with_name("calibration" + suffix + roi_config.suffix)


def calibration_rows(work: WorkDir, teams: list[dict]) -> list[dict]:
    """The calibration rows of the teams in the memo, by team name; none when the table is missing or empty."""
    path = calibration_path(roi_step.config_path(work))
    if not path.exists():
        return []
    try:
        rows = calibration.load(path)
    except ValueError as e:
        raise RoiError(2, str(e)) from e
    names = {t["team"]: t["name"] for t in teams}
    return sorted(
        ({"name": names[r.team_id], "source": r.source_ref, "extrapolated": r.extrapolated} for r in rows if r.team_id in names),
        key=lambda r: r["name"],
    )


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
    at = f"through_month = '{month}'"

    c.section = "headline"
    headline = c.stored(head)

    c.section = "return"
    roi_rows = {}
    if _has(con, "roi_team"):
        for team, spend, benefit, mult, lo, hi, verdict in con.execute(
            "SELECT team_id, spend_cents, benefit_cents, roi_multiple, roi_lo, roi_hi, roi_verdict"
            f" FROM roi_team WHERE {at}"
        ).fetchall():
            roi_rows[team] = (spend, benefit, mult, lo, hi, verdict)
    share_rows = {}
    if _has(con, "fct_ai_share_of_payroll"):
        share_rows = {
            team: share for team, share in con.execute(
                "SELECT team_id, share FROM fct_ai_share_of_payroll WHERE month = ?", [f"{month}-01"]
            ).fetchall()
        }

    def share_num(team: str | None) -> Num | None:
        if team not in share_rows or share_rows[team] is None:
            return None
        who = "company" if team is None else team
        where = "team_id IS NULL" if team is None else f"team_id = '{_q(team)}'"
        share = float(share_rows[team])
        return c.num(f"payroll_share:{who}", "fct_ai_share_of_payroll", f"month = DATE '{month}-01' AND {where}",
                     f"{share * 100:.2f}%", share)

    teams = []
    for team, name, status, reason, effect, lo, hi, unit, kind in con.execute(
        "SELECT t.team_id, t.name, e.status, e.reason, e.effect, e.ci_lo, e.ci_hi, t.unit_label, t.kind"
        f" FROM est_team_effect e JOIN dim_team t USING (team_id) WHERE e.{at} ORDER BY t.name"
    ).fetchall():
        row = {"team": team, "name": name, "status": status, "reason": reason, "unit": unit, "kind": kind,
               "share": share_num(team), "measured": status == "measured", "roi": None}
        if status == "measured":
            where = f"{at} AND team_id = '{_q(team)}'"

            def effect_num(label: str, v: float) -> Num:
                return c.num(f"{label}:{team}", "est_team_effect", where, f"{v:.3f}", v)

            row["effect"], row["lo"], row["hi"] = effect_num("effect", effect), effect_num("effect_lo", lo), effect_num("effect_hi", hi)
            if team in roi_rows:
                spend, benefit, mult, rlo, rhi, verdict = roi_rows[team]
                where = f"{at} AND team_id = '{_q(team)}'"

                def roi_num(label: str, model: str, v: float | None, text: str | None = None) -> Num | None:
                    if v is None:
                        return None
                    return c.num(f"{label}:{team}", model, where, text or f"{v:.2f}", v)

                row["roi"] = {
                    "spend": c.num(f"spend:{team}", "roi_team", where, usd(spend), spend / 100),
                    "benefit": c.num(f"benefit:{team}", "roi_team", where, usd(benefit), benefit / 100),
                    "multiple": roi_num("roi", "roi_team", mult),
                    "lo": roi_num("roi_lo", "roi_team", rlo),
                    "hi": roi_num("roi_hi", "roi_team", rhi),
                    "verdict": VERDICT_TEXT.get(verdict, verdict),
                }
        else:
            row["why"] = REASON_TEXT.get(reason, "no comparison group is available") if status == "unmeasured" else None
        teams.append(row)
    company_share = share_num(None)

    sensitivity = {"multipliers": [], "rows": []}
    if _has(con, "roi_sensitivity") and roi_rows:
        grid = con.execute(
            f"SELECT team_id, value_multiplier, roi_multiple, roi_survives_share FROM roi_sensitivity WHERE {at}"
            " ORDER BY team_id, value_multiplier"
        ).fetchall()
        multipliers = sorted({m for _, m, _, _ in grid})
        names = {t["team"]: t["name"] for t in teams}
        cells: dict[str, dict] = {}
        for team, m, mult, share in grid:
            cells.setdefault(team, {"cells": {}, "share": share})["cells"][m] = mult
        sensitivity["multipliers"] = [
            c.num(f"multiplier:{m!r}", "roi_sensitivity", f"{at} AND value_multiplier = {m!r}", f"{m:g}", m)
            for m in multipliers
        ]
        for team in sorted(cells, key=lambda t: names.get(t, t)):
            entry = cells[team]
            where = f"{at} AND team_id = '{_q(team)}'"
            sensitivity["rows"].append({
                "name": names.get(team, team),
                "cells": [
                    c.num(f"sensitivity:{team}:{m!r}", "roi_sensitivity", f"{where} AND value_multiplier = {m!r}",
                          f"{entry['cells'][m]:.2f}", entry["cells"][m]) if entry["cells"].get(m) is not None else None
                    for m in multipliers
                ],
                "survives": c.num(f"survives:{team}", "roi_sensitivity", where, f"{entry['share'] * 100:.0f}%", entry["share"])
                if entry["share"] is not None else None,
            })

    c.section = "allocation"
    allocation = {"budget": None, "rows": [], "degenerate": False}
    if _has(con, "alloc_result"):
        identified = {}
        if _has(con, "fit_curve"):
            identified = dict(con.execute(f"SELECT team_id, identified FROM fit_curve WHERE {at}").fetchall())
        rows = con.execute(
            "SELECT a.team_id, t.name, a.budget_cents, a.amount_cents, a.capped, a.funded_share_in_draws"
            f" FROM alloc_result a JOIN dim_team t USING (team_id) WHERE a.{at} ORDER BY t.name"
        ).fetchall()
        if rows:
            budget = rows[0][2]
            allocation["budget"] = c.num("allocation_budget", "alloc_result", at, usd(budget), budget / 100)
            for team, name, _, amount, capped, funded in rows:
                where = f"{at} AND team_id = '{_q(team)}'"
                allocation["rows"].append({
                    "name": name, "capped": capped, "identified": identified.get(team),
                    "amount": c.num(f"allocation:{team}", "alloc_result", where, usd(amount), amount / 100),
                    "funded": c.num(f"funded_share:{team}", "alloc_result", where, f"{funded * 100:.0f}%", funded),
                })
            funded_rows = [r for r in rows if r[3] > 0]
            allocation["degenerate"] = len(funded_rows) < 2 or all(r[4] for r in funded_rows)

    c.section = "agenda"
    agenda = {"rows": [], "missing": []}
    items = {}
    if _has(con, "agenda_item"):
        items = {r[0]: r for r in con.execute(
            "SELECT team_id, design, n_per_arm, length_months, delta FROM agenda_item WHERE " + at
        ).fetchall()}
    for t in teams:
        if t["status"] != "unmeasured":
            continue
        if t["team"] not in items:
            agenda["missing"].append(t["name"])
            continue
        _, design, n, length, delta = items[t["team"]]
        where = f"{at} AND team_id = '{_q(t['team'])}'"

        def item_num(label: str, v, text: str) -> Num | None:
            return None if v is None else c.num(f"{label}:{t['team']}", "agenda_item", where, text, v)

        agenda["rows"].append({
            "name": t["name"], "reason": t["reason"], "design": DESIGN_TEXT.get(design, design),
            "n": item_num("agenda_n_per_arm", n, f"{n:,}" if n is not None else ""),
            "length": item_num("agenda_length_months", length, f"{length:,}" if length is not None else ""),
            "delta": item_num("agenda_delta", delta, f"{delta:.3f}" if delta is not None else ""),
        })

    c.section = "assumptions"
    assumptions = []
    if config is not None:
        cfg = roi_calc.load_config(config)
        for t in teams:
            tc = cfg.teams.get(t["team"])
            if not t["measured"] or tc is None or tc.value is None:
                continue
            nums = []
            for part in ("low", "base", "high"):
                v = getattr(tc.value, part)
                key = f"value_per_unit.{t['team']}.{part}"
                nums.append(c.num(f"value_{part}:{t['team']}", prov.CONFIG_MODEL,
                                  prov.config_predicate(config.name, key), usd_unit(v), v))
            assumptions.append({"name": t["name"], "unit": t["unit"], "kind": t["kind"], "low": nums[0],
                                "base": nums[1], "high": nums[2]})

    context = {
        "month": month, "headline": headline, "teams": teams, "company_share": company_share,
        "sensitivity": sensitivity, "allocation": allocation, "agenda": agenda, "assumptions": assumptions,
        "unmeasured": [t for t in teams if t["status"] != "measured"], "calibration": calibration_rows(work, teams),
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
