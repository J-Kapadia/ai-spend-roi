"""Load and validate `config/gen.yaml`: the seed, the DGP parameters, the price table and the Beta Co table.

The Beta Co table is entered by the author at gate G1. While it is empty, or a row lacks a
field, loading fails with exit 4 naming the gate, so the generator never invents numbers.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

import yaml

from roi.workdir import RoiError

GATE = "G1"
KINDS = ("additive", "team_level")

TEAM_FIELDS = (
    "team_id",
    "name",
    "metric_id",
    "unit_label",
    "direction",
    "kind",
    "headcount",
    "monthly_payroll_per_head_usd",
    "baseline_output",
    "target_spend_per_head_usd",
    "effect_at_target",
    "noise_sd",
)


@dataclass(frozen=True)
class TeamSpec:
    team_id: str
    name: str
    metric_id: str
    unit_label: str
    direction: int  # +1 higher is better, -1 lower is better
    kind: str  # additive | team_level
    rollout_month: str | None  # YYYY-MM; None for the control team
    headcount: int
    monthly_payroll_per_head_usd: float
    baseline_output: float  # per head per month if additive, else the raw monthly level
    target_spend_per_head_usd: float  # monthly, once the ramp is complete
    effect_at_target: float  # improvement as a fraction (0.12 = 12%) at the target spend
    noise_sd: float  # team-month noise, log points
    calibration_ref: str | None


@dataclass(frozen=True)
class Price:
    provider: str
    model: str
    input_cents_per_mtok: float
    output_cents_per_mtok: float


@dataclass(frozen=True)
class GenConfig:
    seed: int
    generator_version: str
    fixture: bool
    first_month: str
    last_month: str  # last month written to the books
    incoming_month: str  # held-back month: simulated in the panel and truth, not yet in the books
    month_shock_sd: float
    ramp_months: int
    unassigned_share: float  # of the month's team spend, booked to the `unassigned` bucket
    rows_per_user_month: int
    input_cost_share: float
    gl_account: str
    noise_accounts: tuple[str, ...]
    prices: tuple[Price, ...]
    teams: tuple[TeamSpec, ...]


def months_between(first: str, last: str) -> list[str]:
    y, m = int(first[:4]), int(first[5:])
    out = []
    while f"{y:04d}-{m:02d}" <= last:
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def month_start(month: str) -> date:
    return date(int(month[:4]), int(month[5:]), 1)


def _gate(message: str) -> RoiError:
    return RoiError(4, f"{message}; the author enters the Beta Co table at gate {GATE}")


def _team(raw: dict, index: int) -> TeamSpec:
    label = raw.get("team_id", f"row {index + 1}")
    missing = [f for f in TEAM_FIELDS if raw.get(f) is None]
    if missing:
        raise _gate(f"team {label} has no value for {', '.join(missing)}")
    if "rollout_month" not in raw:
        raise _gate(f"team {label} has no rollout_month (use null for the control team)")
    if raw["kind"] not in KINDS:
        raise RoiError(2, f"team {label}: kind must be one of {KINDS}")
    if raw["direction"] not in (1, -1):
        raise RoiError(2, f"team {label}: direction must be 1 or -1")
    if raw["headcount"] < 1 or raw["baseline_output"] <= 0 or raw["noise_sd"] < 0:
        raise RoiError(2, f"team {label}: headcount, baseline_output and noise_sd must be positive")
    if raw["effect_at_target"] <= -1:
        raise RoiError(2, f"team {label}: effect_at_target must be above -1")
    return TeamSpec(
        team_id=str(raw["team_id"]),
        name=str(raw["name"]),
        metric_id=str(raw["metric_id"]),
        unit_label=str(raw["unit_label"]),
        direction=int(raw["direction"]),
        kind=str(raw["kind"]),
        rollout_month=None if raw["rollout_month"] is None else str(raw["rollout_month"]),
        headcount=int(raw["headcount"]),
        monthly_payroll_per_head_usd=float(raw["monthly_payroll_per_head_usd"]),
        baseline_output=float(raw["baseline_output"]),
        target_spend_per_head_usd=float(raw["target_spend_per_head_usd"]),
        effect_at_target=float(raw["effect_at_target"]),
        noise_sd=float(raw["noise_sd"]),
        calibration_ref=None if raw.get("calibration_ref") is None else str(raw["calibration_ref"]),
    )


def load(path: Path) -> GenConfig:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as e:
        raise RoiError(2, f"cannot read generator config {path}: {e.strerror}") from e
    if not isinstance(raw, dict):
        raise RoiError(2, f"{path} is not a YAML mapping")

    rows = raw.get("teams") or []
    if not rows:
        raise _gate(f"{path}: the Beta Co table is empty")
    teams = tuple(_team(r, i) for i, r in enumerate(rows))
    ids = [t.team_id for t in teams]
    if len(set(ids)) != len(ids) or "unassigned" in ids:
        raise RoiError(2, "team ids must be unique and may not be 'unassigned'")

    dgp, months = raw["dgp"], raw["months"]
    first, last, incoming = (str(months[k]) for k in ("first", "last", "incoming"))
    if not first <= last < incoming:
        raise RoiError(2, "months must satisfy first <= last < incoming")
    for t in teams:
        if t.rollout_month is not None and not first <= t.rollout_month <= incoming:
            raise RoiError(2, f"team {t.team_id}: rollout_month {t.rollout_month} is outside the panel")
    if sum(t.rollout_month is None for t in teams) != 1:
        raise RoiError(2, "exactly one team must be the control (rollout_month null)")

    prices = tuple(
        Price(p["provider"], p["model"], float(p["input_cents_per_mtok"]), float(p["output_cents_per_mtok"]))
        for p in raw["prices"]
    )
    return GenConfig(
        seed=int(raw["seed"]),
        generator_version=str(raw["generator_version"]),
        fixture=bool(raw.get("fixture", False)),
        first_month=first,
        last_month=last,
        incoming_month=incoming,
        month_shock_sd=float(dgp["month_shock_sd"]),
        ramp_months=int(dgp["ramp_months"]),
        unassigned_share=float(dgp["unassigned_share"]),
        rows_per_user_month=int(dgp["rows_per_user_month"]),
        input_cost_share=float(dgp["input_cost_share"]),
        gl_account=str(dgp["gl_account"]),
        noise_accounts=tuple(str(a) for a in dgp["noise_accounts"]),
        prices=prices,
        teams=teams,
    )
