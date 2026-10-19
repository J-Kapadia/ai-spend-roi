"""The published benchmark the memo sets the company beside: the Ramp AI Index share of US businesses paying for AI tools.

The figures are read from the mart `fct_benchmark_ramp`, one row per month and metric, and never from the
reference file behind it. `enumerate_numbers` lists the numbers of the benchmark section (the index, the
figure a year earlier when there is one, and the company's own counts and share), each backed by the
mart row of the month. When the mart is absent or has no row for the month the list is empty and the
memo says so in a sentence.
"""

from __future__ import annotations

from typing import Mapping

import duckdb

from roi import numbers

METRIC = "paid_adoption_share"
SECTION = "benchmark"
MODEL = "fct_benchmark_ramp"

_COLUMNS = (
    "index_value, prior_value, teams_with_ai, teams, company_share, index_period, prior_period, source_url, retrieved_on"
)


def predicate(month: str) -> str:
    return f"month = DATE '{month}-01' AND metric = '{METRIC}'"


def _row(con: duckdb.DuckDBPyConnection, month: str) -> tuple | None:
    """The mart row of `month`, or None when the table is absent or the row has no index or company figure."""
    if not numbers.has_table(con, MODEL):
        return None
    row = con.execute(f"SELECT {_COLUMNS} FROM {MODEL} WHERE {predicate(month)} ORDER BY ALL LIMIT 1").fetchone()
    if row is None or any(row[i] is None for i in (0, 2, 3, 4)):
        return None
    return row


def enumerate_numbers(con: duckdb.DuckDBPyConnection, month: str) -> list[numbers.Number]:
    """The benchmark numbers of `month` in the order the section prints them; empty when there is no row."""
    row = _row(con, month)
    if row is None:
        return []
    index, prior, teams_with_ai, teams, share = row[:5]
    where = predicate(month)
    out: list[numbers.Number] = []

    def add(name: str, display: str, value: float) -> None:
        out.append(numbers.Number(name, SECTION, MODEL, where, display, value))

    add("benchmark_index", f"{float(index):.1f}%", float(index))
    if prior is not None:
        add("benchmark_prior", f"{float(prior):.1f}%", float(prior))
    add("benchmark_teams_with_ai", f"{teams_with_ai:,}", teams_with_ai)
    add("benchmark_teams", f"{teams:,}", teams)
    add("benchmark_company_share", f"{float(share) * 100:.0f}%", float(share))
    return out


def context(con: duckdb.DuckDBPyConnection, month: str, by_name: Mapping[str, object]) -> dict | None:
    """The template context of the section: the numbers to print (looked up by name) and the dates and source."""
    row = _row(con, month)
    if row is None or "benchmark_index" not in by_name:
        return None
    index_period, prior_period, source_url, retrieved_on = row[5:]
    return {
        "index": by_name["benchmark_index"],
        "prior": by_name.get("benchmark_prior"),
        "teams_with_ai": by_name["benchmark_teams_with_ai"],
        "teams": by_name["benchmark_teams"],
        "company_share": by_name["benchmark_company_share"],
        "index_period": index_period,
        "prior_period": prior_period,
        "retrieved_on": str(retrieved_on),
        "source_url": source_url,
    }
