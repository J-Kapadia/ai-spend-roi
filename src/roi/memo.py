"""Render the static memo site and check that every number in it resolves (plan D-14, D-17)."""

from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path

import duckdb
import jinja2

from roi import prov
from roi.steps.provenance import SPEND_NAME
from roi.workdir import RoiError, WorkDir

# A "number" in the memo body (D-17).
NUMBER = re.compile(r"[$-]?\d[\d,]*(\.\d+)?%?")
ROW_CAP = 200
VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}

_env = jinja2.Environment(
    loader=jinja2.PackageLoader("roi", "templates"), autoescape=True, keep_trailing_newline=True,
    trim_blocks=True, lstrip_blocks=True,
)


def open_warehouse(work: WorkDir) -> duckdb.DuckDBPyConnection:
    if not work.warehouse_db.exists():
        raise RoiError(2, f"no warehouse at {work.warehouse_db}; run `roi build` first")
    return duckdb.connect(str(work.warehouse_db), read_only=True)


def build_site(work: WorkDir, month: str, con: duckdb.DuckDBPyConnection) -> Path:
    rec = prov.fetch_named(con, SPEND_NAME, month)
    if rec is None:
        raise RoiError(2, f"no results for month {month}")
    teams = con.execute(
        "SELECT t.name, e.status, e.reason FROM est_team_effect e JOIN dim_team t USING (team_id)"
        " WHERE e.through_month = ? ORDER BY t.name",
        [month],
    ).fetchall()

    prov_dir = work.site / "prov"
    prov_dir.mkdir(parents=True, exist_ok=True)
    columns, rows = prov.source_rows(con, rec.source_model, rec.source_predicate)
    pages = {
        work.site / "index.html": _env.get_template("memo.html.j2").render(month=month, rec=rec, teams=teams),
        prov_dir / f"{rec.prov_id}.html": _env.get_template("prov.html.j2").render(
            rec=rec,
            month=month,
            columns=columns,
            rows=[[prov._fmt(v) for v in r] for r in rows[:ROW_CAP]],
            capped=len(rows) > ROW_CAP,
        ),
    }
    for path, html in pages.items():
        path.write_text(html, encoding="utf-8")
    return work.site / "index.html"


class _MemoScan(HTMLParser):
    """DOM walk over the body: collects tagged numbers and numbers outside any tag."""

    def __init__(self) -> None:
        super().__init__()
        self.stack: list[tuple[str, dict[str, str | None]]] = []
        self.tagged: list[dict] = []
        self.untagged: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in VOID_TAGS:
            return
        a = dict(attrs)
        if "data-prov" in a:
            anchor = next((p[1] for p in reversed(self.stack) if p[0] == "a"), {})
            self.tagged.append({"id": a["data-prov"], "href": anchor.get("href"), "text": ""})
        self.stack.append((tag, a))

    def handle_endtag(self, tag):
        while self.stack:
            if self.stack.pop()[0] == tag:
                break

    def _exempt(self) -> bool:
        for tag, a in self.stack:
            if tag in {"time", "script", "style"} or "data-prov" in a or "lit" in (a.get("class") or "").split():
                return True
        return False

    def handle_data(self, data):
        if not any(tag == "body" for tag, _ in self.stack):
            return
        in_tag = next((t for t in reversed(self.tagged) if any("data-prov" in a for _, a in self.stack)), None)
        if in_tag is not None:
            in_tag["text"] += data
        if not self._exempt():
            self.untagged += [m.group(0) for m in NUMBER.finditer(data)]


class _ProvPage(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.attrs: dict[str, str | None] = {}

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if "data-prov-page" in a:
            self.attrs = a


def check_site(work: WorkDir, month: str, con: duckdb.DuckDBPyConnection) -> list[str]:
    """Return resolved prov ids; raise RoiError(1, ...) on the first failure."""
    scan = _MemoScan()
    scan.feed((work.site / "index.html").read_text(encoding="utf-8"))
    if scan.untagged:
        raise RoiError(1, f"untagged number in memo body: {scan.untagged[0]}")
    if not scan.tagged:
        raise RoiError(1, "memo has no tagged numbers")
    for tag in scan.tagged:
        rec = prov.fetch(con, tag["id"])
        if rec is None:
            raise RoiError(1, f"number {tag['text']!r} has no provenance record {tag['id']}")
        rows, digest = prov.recheck(con, rec)
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
    return [t["id"] for t in scan.tagged]
