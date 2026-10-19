"""The return table of a built site carries no figure for a team without a measurement."""

from __future__ import annotations

import re
from html.parser import HTMLParser

import duckdb

from roi import estimator, verify_books
from roi.checks import CheckResult
from roi.workdir import WorkDir

NAME = "memo_table"
ORDER = 60
# Columns that may show a number for any team: they are not estimates.
NOT_ESTIMATES = {"Team", "Status", "Share of payroll"}


class _ReturnTable(HTMLParser):
    """The cells of `table#return`: (text, whether a tagged number is inside) per cell, per row."""

    def __init__(self) -> None:
        super().__init__()
        self.rows: list[list[tuple[str, bool]]] = []
        self.month: str | None = None
        self._in_table = False
        self._cell: list | None = None  # [text, tagged]

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "time" and self.month is None and a.get("datetime"):
            self.month = a["datetime"]
        if tag == "table":
            self._in_table = a.get("id") == "return"
        elif self._in_table and tag == "tr":
            self.rows.append([])
        elif self._in_table and tag in ("td", "th"):
            self._cell = ["", False]
        if self._cell is not None and "data-prov" in a:
            self._cell[1] = True

    def handle_endtag(self, tag):
        if tag == "table":
            self._in_table = False
        elif tag in ("td", "th") and self._cell is not None:
            self.rows[-1].append((self._cell[0].strip(), self._cell[1]))
            self._cell = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell[0] += data


def run(con: duckdb.DuckDBPyConnection, through: str, work: WorkDir) -> CheckResult:
    index = work.site / "index.html"
    if not index.exists():
        return CheckResult(NAME, note="no site built")
    table = _ReturnTable()
    table.feed(index.read_text(encoding="utf-8"))
    if table.month is not None and table.month != through:
        return CheckResult(NAME, note=f"the site is for {table.month}, not {through}")
    verify_books.require(con, "dim_team")
    est = verify_books.team_effects(con, through)
    names = dict(con.execute("SELECT name, team_id FROM dim_team").fetchall())
    header, *rows = table.rows or [[]]
    figures = [i for i, (text, _) in enumerate(header) if text not in NOT_ESTIMATES]
    failures: list[str] = []
    for row in rows:
        team = names.get(row[0][0]) if row else None
        status = est.get(team, {}).get("status")
        if status is None or status == estimator.MEASURED:
            continue  # the company line, or a team the table may show figures for
        for i in figures:
            if i < len(row) and (row[i][1] or re.search(r"\d", row[i][0])):
                failures.append(
                    f"team={team} is {status} and the return table shows {row[i][0]!r} under {header[i][0]!r}"
                )
    return CheckResult(NAME, tuple(failures))
