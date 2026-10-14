"""Toy generator: 3 teams, 6 months, from a fixed seed.

A library function that `roi build --toy` calls, not a command. It writes the books to
`data/books/` and a small `truth/truth.json` with its own serializer. Throwaway: the
Beta Co generator replaces it.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import numpy as np

from roi.workdir import WorkDir

TOY_SEED = 1
TOY_MONTHS = tuple(date(2025, m, 1) for m in range(1, 7))
TOY_THROUGH = "2025-06"
HEADCOUNT = 4
PAYROLL_CENTS = 800_000
TARGET_SPEND_CENTS = 20_000  # per head per month once a team has AI tools
GL_ACCOUNT = "6100"
NOISE_SD = 0.02
MONTH_SHOCK_SD = 0.03


@dataclass(frozen=True)
class Team:
    team_id: str
    name: str
    metric_id: str
    unit_label: str
    base_output: float  # per head per month
    rollout: date | None  # None: the holdout, never treated
    effect: float  # planted effect in log points


TEAMS = (
    Team("alpha", "Alpha", "tickets_resolved", "tickets", 50.0, date(2025, 3, 1), 0.10),
    Team("beta", "Beta", "changes_shipped", "changes", 20.0, date(2025, 4, 1), 0.20),
    Team("gamma", "Gamma", "requisitions_filled", "requisitions", 5.0, None, 0.0),
)


def _write_csv(path: Path, header: list[str], rows: list[list]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(header)
        w.writerows(rows)


def generate(work: WorkDir) -> None:
    """Write the toy books and truth under `work`. Same seed, same bytes."""
    work.books.mkdir(parents=True, exist_ok=True)
    work.truth.mkdir(parents=True, exist_ok=True)

    children = np.random.SeedSequence(TOY_SEED).spawn(1 + len(TEAMS))
    shock = np.random.Generator(np.random.PCG64(children[0])).normal(0.0, MONTH_SHOCK_SD, len(TOY_MONTHS))

    spend_rows, gl_rows, roster_rows, output_rows, team_rows = [], [], [], [], []
    truth: dict[str, dict] = {}
    for team, child in zip(TEAMS, children[1:]):
        rng = np.random.Generator(np.random.PCG64(child))
        users = [f"{team.team_id}-u{i + 1}" for i in range(HEADCOUNT)]
        team_rows.append([team.team_id, team.name, team.metric_id, team.unit_label, 1, "additive"])
        roster_rows += [[u, team.team_id, "2024-01-01", "", PAYROLL_CENTS] for u in users]
        truth[team.team_id] = {
            "rollout_month": team.rollout.isoformat() if team.rollout else None,
            "planted_effect_by_month": {
                m.isoformat(): team.effect for m in TOY_MONTHS if team.rollout and m >= team.rollout
            },
        }
        for t, month in enumerate(TOY_MONTHS):
            treated = team.rollout is not None and month >= team.rollout
            month_cents = 0
            if treated:
                for i, user in enumerate(users):
                    cents = int(round(TARGET_SPEND_CENTS * rng.uniform(0.8, 1.2)))
                    month_cents += cents
                    spend_rows.append(
                        [(month + timedelta(days=i)).isoformat(), team.team_id, f"{team.team_id}-proj", user,
                         "provider_a", "model_a1", cents * 1000, cents * 200, cents]
                    )
                gl_rows.append([f"gl-{team.team_id}-{month:%Y-%m}", f"{month:%Y-%m}", GL_ACCOUNT,
                                team.team_id, month_cents, 0, "AI usage"])
            level = HEADCOUNT * team.base_output * float(
                np.exp(shock[t] + rng.normal(0.0, NOISE_SD) + (team.effect if treated else 0.0))
            )
            output_rows.append([team.team_id, month.isoformat(), team.metric_id, round(level, 6)])

    _write_csv(work.books / "raw_ai_spend.csv",
               ["usage_date", "team_id", "project_id", "user_id", "provider", "model",
                "input_tokens", "output_tokens", "cost_cents"], spend_rows)
    _write_csv(work.books / "raw_gl_journal.csv",
               ["entry_id", "period", "account_code", "cost_center", "debit_cents", "credit_cents", "memo"], gl_rows)
    _write_csv(work.books / "raw_hr_roster.csv",
               ["employee_id", "team_id", "start_date", "end_date", "monthly_payroll_cents"], roster_rows)
    _write_csv(work.books / "raw_output.csv", ["team_id", "month", "metric_id", "value"], output_rows)
    _write_csv(work.books / "dim_team.csv",
               ["team_id", "name", "metric_id", "unit_label", "direction", "kind"], team_rows)
    (work.truth / "truth.json").write_text(
        json.dumps({"seed": TOY_SEED, "generator_version": "toy-0", "teams": truth}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
