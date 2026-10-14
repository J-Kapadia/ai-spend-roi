"""Load and check `config/calibration.yaml`: one row per team, the published range its planted effect must sit in.

The file holds no figure until a person has read the paper. The schema is checked on
every load; the per-row rules (a `verified_on` date, a numeric range, a source) apply to every
row that exists, so an empty `rows` list is a valid file and a half-filled one is not.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import yaml

TOP_KEYS = ("rows",)
ROW_KEYS = ("team_id", "effect_low", "effect_high", "source_ref", "figure", "verified_on", "extrapolated")


@dataclass(frozen=True)
class Row:
    team_id: str
    effect_low: float  # improvement as a fraction (0.12 = 12%), the same unit as `effect_at_target`
    effect_high: float
    source_ref: str  # authors, year, venue
    figure: str  # as printed in the paper
    verified_on: str  # ISO date a person checked the figure against the paper
    extrapolated: bool  # true when the study's task differs from the team's


def read(path: Path) -> dict:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    return raw if isinstance(raw, dict) else {}


def _is_number(v: object) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _iso_date(v: object) -> bool:
    if isinstance(v, date):  # YAML turns an unquoted 2026-10-13 into a date
        return True
    if not (isinstance(v, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", v)):
        return False
    try:
        date.fromisoformat(v)
    except ValueError:
        return False
    return True


def schema_problems(raw: dict) -> list[str]:
    """Problems with the shape of the file, whatever it holds: unknown or missing keys, wrong types."""
    problems = [f"unknown top-level key {k!r}" for k in raw if k not in TOP_KEYS]
    rows = raw.get("rows")
    if not isinstance(rows, list):
        return problems + ["`rows` must be a list (empty until a person has checked the paper)"]
    for i, row in enumerate(rows, 1):
        label = f"row {i}"
        if not isinstance(row, dict):
            problems.append(f"{label} is not a mapping")
            continue
        label = f"row {i} ({row.get('team_id')})"
        problems += [f"{label}: missing {k}" for k in ROW_KEYS if k not in row]
        problems += [f"{label}: unknown key {k!r}" for k in row if k not in ROW_KEYS]
    return problems


def row_problems(raw: dict) -> list[str]:
    """Problems with the content of every row that exists. Run after `schema_problems` is empty."""
    problems: list[str] = []
    seen: set[str] = set()
    for i, row in enumerate(raw["rows"], 1):
        label = f"row {i} ({row['team_id']})"
        if not isinstance(row["team_id"], str) or not row["team_id"]:
            problems.append(f"{label}: team_id must be a non-empty string")
        elif row["team_id"] in seen:
            problems.append(f"{label}: duplicate team_id")
        seen.add(str(row["team_id"]))
        if not (_is_number(row["effect_low"]) and _is_number(row["effect_high"])):
            problems.append(f"{label}: effect_low and effect_high must be numbers")
        elif not -1 < row["effect_low"] <= row["effect_high"]:
            problems.append(f"{label}: the range must satisfy -1 < effect_low <= effect_high")
        for key in ("source_ref", "figure"):
            if not isinstance(row[key], str) or not row[key].strip():
                problems.append(f"{label}: {key} must be a non-empty string")
        if row["verified_on"] in (None, ""):
            problems.append(f"{label}: no verified_on; a person has not checked the figure against the paper")
        elif not _iso_date(row["verified_on"]):
            problems.append(f"{label}: verified_on must be an ISO date (YYYY-MM-DD)")
        if not isinstance(row["extrapolated"], bool):
            problems.append(f"{label}: extrapolated must be true or false")
    return problems


def effect_problems(raw: dict, effects: dict[str, float]) -> list[str]:
    """Teams in `effects` ({team_id: planted effect}) with no row, or an effect outside its row's range."""
    ranges = {r["team_id"]: r for r in raw["rows"]}
    problems = []
    for team, effect in sorted(effects.items()):
        row = ranges.get(team)
        if row is None:
            problems.append(f"{team}: no calibration row")
        elif not row["effect_low"] <= effect <= row["effect_high"]:
            problems.append(f"{team}: planted effect {effect} is outside the cited range [{row['effect_low']}, {row['effect_high']}]")
    problems += [f"{t}: calibration row for a team with no planted effect" for t in sorted(set(ranges) - set(effects))]
    return problems


def load(path: Path) -> list[Row]:
    """The rows of a file that passes `schema_problems` and `row_problems`; raises ValueError otherwise."""
    raw = read(path)
    problems = schema_problems(raw) or row_problems(raw)
    if problems:
        raise ValueError(f"{path}: " + "; ".join(problems))
    return [
        Row(r["team_id"], float(r["effect_low"]), float(r["effect_high"]), r["source_ref"], r["figure"],
            str(r["verified_on"]), r["extrapolated"])
        for r in raw["rows"]
    ]
