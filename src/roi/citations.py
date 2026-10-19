"""Load and check `config/citations.yaml`: the public statements the memo may quote.

A quotation enters the memo only from a row that carries `verified_on`; rows are matched to
`config/calibration.yaml` by `source_ref` string equality. The schema is checked on every load,
and a half-filled row is an error, so a quote cannot reach the memo unchecked.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import yaml

TOP_KEYS = ("rows",)
REQUIRED = ("key", "source_ref", "url", "retrieved_on")
OPTIONAL = ("verified_on", "quote")  # a row without verified_on is kept but never rendered as a quote


@dataclass(frozen=True)
class Citation:
    key: str
    source_ref: str  # as it appears in config/calibration.yaml
    url: str  # the DOI or publisher link
    retrieved_on: str  # ISO date
    verified_on: str | None  # ISO date the quote was verified against the source's text; None when not verified
    quote: str | None


def _iso_date(v: object) -> bool:
    if isinstance(v, date):  # YAML turns an unquoted 2026-10-14 into a date
        return True
    if not (isinstance(v, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", v)):
        return False
    try:
        date.fromisoformat(v)
    except ValueError:
        return False
    return True


def _problems(raw: object) -> list[str]:
    if not isinstance(raw, dict):
        return ["the file must be a mapping with a `rows` list"]
    problems = [f"unknown top-level key {k!r}" for k in raw if k not in TOP_KEYS]
    rows = raw.get("rows")
    if not isinstance(rows, list):
        return problems + ["`rows` must be a list"]
    seen: set[str] = set()
    for i, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            problems.append(f"row {i} is not a mapping")
            continue
        label = f"row {i} ({row.get('key')})"
        problems += [f"{label}: missing {k}" for k in REQUIRED if k not in row]
        problems += [f"{label}: unknown key {k!r}" for k in row if k not in REQUIRED + OPTIONAL]
        for k in ("key", "source_ref", "url"):
            if k in row and (not isinstance(row[k], str) or not row[k].strip()):
                problems.append(f"{label}: {k} must be a non-empty string")
        if isinstance(row.get("key"), str):
            if row["key"] in seen:
                problems.append(f"{label}: duplicate key")
            seen.add(row["key"])
        if isinstance(row.get("url"), str) and row["url"].strip() and not row["url"].startswith("https://"):
            problems.append(f"{label}: url must start with https://")
        for k in ("retrieved_on", "verified_on"):
            if k in row and not (k == "verified_on" and row[k] is None) and not _iso_date(row[k]):
                problems.append(f"{label}: {k} must be an ISO date (YYYY-MM-DD)")
        if "quote" in row and (not isinstance(row["quote"], str) or not row["quote"].strip()):
            problems.append(f"{label}: quote must be a non-empty string when present")
    return problems


def load(path: Path) -> list[Citation]:
    """The rows of a file that passes the shape checks; raises ValueError otherwise."""
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise ValueError(f"{path}: not valid YAML: {e}") from e
    problems = _problems(raw)
    if problems:
        raise ValueError(f"{path}: " + "; ".join(problems))
    return [
        Citation(r["key"], r["source_ref"], r["url"], str(r["retrieved_on"]),
                 None if r.get("verified_on") is None else str(r["verified_on"]), r.get("quote"))
        for r in raw["rows"]
    ]


def for_source(citations: list[Citation], source_ref: str) -> Citation | None:
    """The row whose `source_ref` equals `source_ref`, or None."""
    return next((c for c in citations if c.source_ref == source_ref), None)


def verified(citations: list[Citation]) -> list[Citation]:
    """The rows that carry `verified_on`: the only ones whose quote or link the memo may print."""
    return [c for c in citations if c.verified_on]
