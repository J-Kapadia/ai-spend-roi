"""Simulate the team-month panel: spend, headcount and outcome per team and month.

Pure functions of a `GenConfig` and a seed; no files are read or written, so the coverage
simulation can call `simulate` with many seeds. `roi.gen.books` renders the panel into rows.

Model, in the direction-normalised log outcome Y = direction * ln(outcome):
    Y[team, t] = direction * ln(baseline) + shock[t] + effect[team, t] + noise[team, t]
`shock` is one draw per month common to every team; `noise` is per team-month. A team's
spend per head ramps linearly over its first `ramp_months` of rollout to its target and has
no other noise. The planted effect is A * (1 - exp(-x / k)) with x the month's spend per
head, k the target spend per head, and A fixed so that the effect at the target equals
ln(1 + effect_at_target).
"""

from __future__ import annotations

import math
import zlib
from dataclasses import dataclass

import numpy as np

from roi.gen.config import GenConfig, TeamSpec, months_between

# Independent random streams: the stream id is the second entropy word, so a draw never
# depends on how many other streams exist or in which order teams are listed.
STREAM_SHOCK, STREAM_TEAM, STREAM_BOOKS, STREAM_UNASSIGNED, STREAM_GL = 1, 2, 3, 4, 5


def stream(seed: int, kind: int, team_id: str | None = None) -> np.random.Generator:
    key = [seed, kind] + ([zlib.crc32(team_id.encode())] if team_id is not None else [])
    return np.random.Generator(np.random.PCG64(np.random.SeedSequence(key)))


@dataclass(frozen=True)
class TeamPanel:
    spec: TeamSpec
    spend_cents: np.ndarray  # int64 per month
    effect: np.ndarray  # planted effect in log points, in the direction-normalised outcome
    outcome: np.ndarray  # reported level: per head if additive, else the raw level
    output: np.ndarray  # the value written to the output books: outcome * headcount if additive
    curve_a: float
    curve_k: float  # dollars of monthly spend per head


@dataclass(frozen=True)
class Panel:
    seed: int
    months: tuple[str, ...]  # first_month .. incoming_month
    shock: np.ndarray
    teams: tuple[TeamPanel, ...]
    unassigned_cents: np.ndarray  # int64 per month

    def team(self, team_id: str) -> TeamPanel:
        return next(t for t in self.teams if t.spec.team_id == team_id)


def curve_a(effect_at_target: float) -> float:
    """Asymptote A such that A * (1 - exp(-1)) = ln(1 + figure): at its target a team is at k."""
    return math.log1p(effect_at_target) / (1.0 - math.exp(-1.0))


def _team_panel(cfg: GenConfig, spec: TeamSpec, months: list[str], shock: np.ndarray, seed: int) -> TeamPanel:
    n = len(months)
    a, k = curve_a(spec.effect_at_target), spec.target_spend_per_head_usd
    spend = np.zeros(n, dtype=np.int64)
    if spec.rollout_month is not None:
        start = months.index(spec.rollout_month)
        for t in range(start, n):
            ramp = min(1.0, (t - start + 1) / cfg.ramp_months)
            spend[t] = int(round(spec.headcount * k * 100 * ramp))
    per_head_usd = spend / spec.headcount / 100.0
    effect = a * (1.0 - np.exp(-per_head_usd / k)) if spec.rollout_month is not None else np.zeros(n)
    noise = stream(seed, STREAM_TEAM, spec.team_id).normal(0.0, spec.noise_sd, n)
    y = spec.direction * math.log(spec.baseline_output) + shock + effect + noise
    outcome = np.exp(spec.direction * y)
    output = outcome * spec.headcount if spec.kind == "additive" else outcome
    return TeamPanel(spec, spend, effect, outcome, output, a, k)


def simulate(cfg: GenConfig, seed: int | None = None) -> Panel:
    seed = cfg.seed if seed is None else seed
    months = months_between(cfg.first_month, cfg.incoming_month)
    shock = stream(seed, STREAM_SHOCK).normal(0.0, cfg.month_shock_sd, len(months))
    teams = tuple(_team_panel(cfg, spec, months, shock, seed) for spec in cfg.teams)
    team_total = sum(t.spend_cents for t in teams)
    unassigned = np.array([int(round(cfg.unassigned_share * int(c))) for c in team_total], dtype=np.int64)
    return Panel(seed, tuple(months), shock, teams, unassigned)
