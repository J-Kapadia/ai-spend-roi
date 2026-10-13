"""ROI multiple, its interval, its sensitivity to the value per unit, and the verdict.

Pure functions over a team's estimated effect and its window of months; the only I/O is
`read_inputs`, which reads the estimator's result tables and the marts from a DuckDB
connection the caller opened. Nothing here reads the truth file.

The team effect is in log points of Y = direction x ln(outcome). It converts to a lift in the
metric's own units with the exact formula (the baseline is the team's mean outcome before
rollout):

    direction = +1   unit lift = baseline x (e^effect - 1)
    direction = -1   unit lift = baseline x (1 - e^-effect)

and the benefit of the window is the lift times the exposure times the value per unit, in
integer cents, rounded half-even once after the float multiplication:

    additive      exposure = headcount-months of the window     (the lift is per head per month)
    team_level    exposure = months of the window               (the metric is a level for the team)

The window is the months of the valid group-time cells the team effect averages over. The ROI
multiple is benefit over the team's AI spend in the same window; 1.0 is break-even. The
interval is the effect's interval converted the same way, at the base value only; the range of
the value appears only in the sensitivity: a grid of global multipliers, and the share of
independent uniform draws of the value in which the multiple exceeds 1. The draws are the ones
`roi.alloc` uses for its funded share, so both questions are answered from one set.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import yaml

from roi import alloc
from roi.workdir import RoiError

ADDITIVE, TEAM_LEVEL = "additive", "team_level"
KINDS = (ADDITIVE, TEAM_LEVEL)

GOOD, POOR, NOT_DISTINGUISHABLE = "good", "poor", "not_distinguishable"
VERDICTS = (GOOD, POOR, NOT_DISTINGUISHABLE)

DEFAULT_MULTIPLIERS = (0.5, 0.75, 1.0, 1.25, 1.5)
DEFAULT_SURVIVE_SHARE = 0.90  # a team survives when its multiple exceeds 1 in this share of the draws


@dataclass(frozen=True)
class Value:
    """USD per unit of output (per unit-month of change for a team-level metric)."""

    low: float
    base: float
    high: float


@dataclass(frozen=True)
class TeamConfig:
    kind: str | None  # as the books say it; None when the config does not state it
    value: Value | None  # None until the author enters it


@dataclass(frozen=True)
class RoiConfig:
    teams: Mapping[str, TeamConfig]
    multipliers: tuple[float, ...]
    survive_share: float
    path: Path


@dataclass(frozen=True)
class TeamInput:
    """A measured team as the estimator left it, with its window."""

    team_id: str
    kind: str
    direction: int
    effect: float
    ci_lo: float
    ci_hi: float
    baseline: float
    headcounts: tuple[int, ...]  # one per month of the window
    spend_cents: int  # AI spend over the window

    @property
    def exposure(self) -> float:
        return exposure(self.kind, self.headcounts)


@dataclass(frozen=True)
class TeamRoi:
    team_id: str
    benefit_cents: int
    spend_cents: int
    roi_multiple: float | None  # None when the window has no spend
    roi_lo: float | None
    roi_hi: float | None
    breakeven_value_cents: int | None  # cents per unit at which the multiple is 1; None without a positive lift
    baseline_outcome: float
    roi_survives_share: float | None
    grid: tuple[tuple[float, float | None], ...]  # (value multiplier, ROI multiple)
    verdict: str


def load_config(path: Path) -> RoiConfig:
    """Read the roi config. A malformed file is a usage error (exit 2); empty values are not an
    error here, because only a measured team needs them (see the build step)."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as e:
        raise RoiError(2, f"cannot read the roi config {path}: {e.strerror}") from None
    except yaml.YAMLError as e:
        raise RoiError(2, f"the roi config {path} is not valid YAML: {e}") from None
    if not isinstance(raw, dict) or not isinstance(raw.get("value_per_unit"), dict):
        raise RoiError(2, f"the roi config {path} has no `value_per_unit` table")
    sens = raw.get("sensitivity") or {}
    try:
        multipliers = tuple(float(m) for m in sens.get("multipliers", DEFAULT_MULTIPLIERS))
        survive = float(sens.get("survive_share", DEFAULT_SURVIVE_SHARE))
    except (TypeError, ValueError):
        raise RoiError(2, f"the roi config {path} has a malformed `sensitivity` table") from None
    if not multipliers or not 0.0 < survive <= 1.0:
        raise RoiError(2, f"the roi config {path} has a bad `sensitivity` table")
    teams: dict[str, TeamConfig] = {}
    for team, spec in raw["value_per_unit"].items():
        spec = spec or {}
        kind = spec.get("kind")
        if kind is not None and kind not in KINDS:
            raise RoiError(2, f"the roi config has kind {kind!r} for {team}; expected one of {', '.join(KINDS)}")
        teams[str(team)] = TeamConfig(kind, _value(str(team), spec))
    return RoiConfig(teams, multipliers, survive, path)


def _value(team: str, raw: dict) -> Value | None:
    parts = [raw.get(k) for k in ("low", "base", "high")]
    if all(p is None for p in parts):
        return None
    if any(p is None for p in parts):
        return None  # partly entered counts as not entered; the gate names the team
    try:
        low, base, high = (float(p) for p in parts)
    except (TypeError, ValueError):
        raise RoiError(2, f"the value per unit of {team} must be numbers") from None
    if not 0.0 <= low <= base <= high:
        raise RoiError(2, f"the value per unit of {team} must satisfy 0 <= low <= base <= high")
    return Value(low, base, high)


def exposure(kind: str, headcounts: Sequence[int]) -> float:
    return float(sum(headcounts)) if kind == ADDITIVE else float(len(headcounts))


def unit_lift(effect: float, baseline: float, direction: int) -> float:
    """The effect in log points as a lift in the metric's units (per head if additive)."""
    if direction == 1:
        return baseline * math.expm1(effect)
    return baseline * -math.expm1(-effect)


def benefit_cents(lift: float, expo: float, value_usd: float) -> int:
    """Dollars of output value over the window, in cents, rounded half-even once."""
    return int(round(lift * expo * value_usd * 100.0))


def _multiple(benefit: int, spend_cents: int) -> float | None:
    return None if spend_cents == 0 else benefit / spend_cents


def value_draws(team_id: str, low: float, high: float) -> np.ndarray:
    """The team's draws of its value per unit, uniform between low and high: the unit-interval draws
    of `roi.alloc` (one stream per team, independent of team order) scaled to the team's range."""
    return low + (high - low) * alloc.value_draws(team_id)


def survives_share(draws: np.ndarray, units: float, spend_cents: int) -> float | None:
    """Share of draws (USD per unit) in which `units` of lift pay back more than `spend_cents`."""
    if spend_cents == 0:
        return None
    return float(np.mean(draws * units * 100.0 > spend_cents))


def verdict(roi_lo: float | None, roi_hi: float | None, share: float | None, threshold: float) -> str:
    """`good` only when the whole interval is above break-even and the value range agrees;
    `poor` when the whole interval is below it; otherwise the data do not tell."""
    if roi_lo is not None and share is not None and roi_lo > 1.0 and share >= threshold:
        return GOOD
    if roi_hi is not None and roi_hi < 1.0:
        return POOR
    return NOT_DISTINGUISHABLE


def compute_team(inp: TeamInput, value: Value, cfg: RoiConfig) -> TeamRoi:
    expo = inp.exposure
    lift = unit_lift(inp.effect, inp.baseline, inp.direction)
    benefit = benefit_cents(lift, expo, value.base)
    spend = inp.spend_cents

    def at_effect(effect: float) -> float | None:
        return _multiple(benefit_cents(unit_lift(effect, inp.baseline, inp.direction), expo, value.base), spend)

    units = lift * expo
    share = survives_share(value_draws(inp.team_id, value.low, value.high), units, spend)
    roi_lo, roi_hi = at_effect(inp.ci_lo), at_effect(inp.ci_hi)
    grid = tuple((m, _multiple(benefit_cents(lift, expo, value.base * m), spend)) for m in cfg.multipliers)
    return TeamRoi(
        team_id=inp.team_id,
        benefit_cents=benefit,
        spend_cents=spend,
        roi_multiple=_multiple(benefit, spend),
        roi_lo=roi_lo,
        roi_hi=roi_hi,
        breakeven_value_cents=int(round(spend / units)) if units > 0 else None,
        baseline_outcome=inp.baseline,
        roi_survives_share=share,
        grid=grid,
        verdict=verdict(roi_lo, roi_hi, share, cfg.survive_share),
    )


def read_inputs(con, through: str) -> list[TeamInput]:
    """The measured teams of `through` from the estimator's tables, with the headcount and AI
    spend of each month of the team's window (the months of its group-time cells)."""
    teams = con.execute(
        "SELECT e.team_id, d.kind, d.direction, e.effect, e.ci_lo, e.ci_hi, e.baseline_outcome"
        " FROM est_team_effect e JOIN dim_team d USING (team_id)"
        " WHERE e.through_month = ? AND e.status = 'measured' ORDER BY e.team_id",
        [through],
    ).fetchall()
    out = []
    for team, kind, direction, effect, ci_lo, ci_hi, baseline in teams:
        window = con.execute(
            "SELECT f.headcount, f.spend_cents FROM est_att_gt a"
            " JOIN fct_team_month f ON f.team_id = a.team_id AND f.month = a.t"
            " WHERE a.through_month = ? AND a.team_id = ? ORDER BY a.t",
            [through, team],
        ).fetchall()
        out.append(TeamInput(
            team_id=team, kind=kind, direction=int(direction), effect=effect, ci_lo=ci_lo, ci_hi=ci_hi,
            baseline=baseline, headcounts=tuple(int(h) for h, _ in window),
            spend_cents=int(sum(s for _, s in window)),
        ))
    return out
