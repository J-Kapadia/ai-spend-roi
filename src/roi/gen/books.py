"""Render a simulated panel into the books (CSV) and the truth file.

Every team-month's spend is split into user-day rows that sum exactly to the panel's integer
cents; the general ledger carries one AI-usage line per team-month (plus the `unassigned`
bucket) equal to that sum, and a few lines on other accounts the pipeline must ignore.

The truth file is written here with its own serializer; this module never imports `roi.truth`.
"""

from __future__ import annotations

import calendar
import csv
import json
from datetime import date
from pathlib import Path

import numpy as np

from roi.gen import manifest
from roi.gen.config import GenConfig, month_start
from roi.gen.panel import STREAM_BOOKS, STREAM_GL, STREAM_UNASSIGNED, Panel, stream
from roi.workdir import WorkDir

UNASSIGNED = "unassigned"
BOOK_FILES = ("raw_ai_spend", "raw_gl_journal", "raw_hr_roster", "raw_output", "dim_team")


def split_cents(total: int, weights: np.ndarray) -> list[int]:
    """Split `total` cents in proportion to `weights`; the parts are integers that sum to `total`."""
    exact = total * weights / weights.sum()
    parts = np.floor(exact).astype(np.int64)
    short = total - int(parts.sum())
    for i in sorted(range(len(parts)), key=lambda i: (-(exact[i] - parts[i]), i))[:short]:
        parts[i] += 1
    return [int(p) for p in parts]


def _write_csv(path: Path, header: list[str], rows: list[list]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(header)
        w.writerows(rows)


def _spend_rows(cfg: GenConfig, panel: Panel, months: list[str]) -> list[list]:
    rows: list[list] = []
    prices = cfg.prices
    for tp in panel.teams:
        spec = tp.spec
        rng = stream(panel.seed, STREAM_BOOKS, spec.team_id)
        for t, month in enumerate(panel.months):
            if month not in months or tp.spend_cents[t] == 0:
                continue
            first = month_start(month)
            ndays = calendar.monthrange(first.year, first.month)[1]
            cells = []  # (day, user index) for each row of the month
            for ui in range(spec.headcount):
                days = rng.choice(ndays, size=min(cfg.rows_per_user_month, ndays), replace=False)
                cells += [(int(d) + 1, ui) for d in sorted(days)]
            weights = rng.uniform(0.5, 1.5, len(cells))
            for (day, ui), cents in zip(cells, split_cents(int(tp.spend_cents[t]), weights)):
                price = prices[ui % len(prices)]
                rows.append(_row(date(first.year, first.month, day), spec.team_id,
                                 f"{spec.team_id}-p{1 + ui % 2}", f"{spec.team_id}-e{ui + 1:03d}", price, cents,
                                 cfg.input_cost_share))
    rng = stream(panel.seed, STREAM_UNASSIGNED)
    for t, month in enumerate(panel.months):
        if month not in months or panel.unassigned_cents[t] == 0:
            continue
        first = month_start(month)
        ndays = calendar.monthrange(first.year, first.month)[1]
        days = sorted(int(d) + 1 for d in rng.choice(ndays, size=min(cfg.rows_per_user_month, ndays), replace=False))
        weights = rng.uniform(0.5, 1.5, len(days))
        for day, cents in zip(days, split_cents(int(panel.unassigned_cents[t]), weights)):
            rows.append(_row(date(first.year, first.month, day), UNASSIGNED, f"{UNASSIGNED}-p1",
                             f"{UNASSIGNED}-u1", prices[0], cents, cfg.input_cost_share))
    rows.sort(key=lambda r: (r[0], r[1], r[3]))
    return rows


def _row(day: date, team_id: str, project: str, user: str, price, cents: int, input_share: float) -> list:
    tokens_in = int(round(cents * input_share / price.input_cents_per_mtok * 1e6))
    tokens_out = int(round(cents * (1 - input_share) / price.output_cents_per_mtok * 1e6))
    return [day.isoformat(), team_id, project, user, price.provider, price.model, tokens_in, tokens_out, cents]


def _gl_rows(cfg: GenConfig, panel: Panel, months: list[str]) -> list[list]:
    rng = stream(panel.seed, STREAM_GL)
    rows: list[list] = []
    for t, month in enumerate(panel.months):
        if month not in months:
            continue
        buckets = [(tp.spec.team_id, int(tp.spend_cents[t])) for tp in panel.teams]
        buckets.append((UNASSIGNED, int(panel.unassigned_cents[t])))
        for cost_center, cents in buckets:
            if cents:
                rows.append([f"gl-{cost_center}-{month}-{cfg.gl_account}", month, cfg.gl_account,
                             cost_center, cents, 0, "AI usage"])
            if cost_center == UNASSIGNED:
                continue
            for account in cfg.noise_accounts:
                amount = int(rng.integers(50_000, 500_000))
                rows.append([f"gl-{cost_center}-{month}-{account}", month, account, cost_center,
                             amount, 0, "Other operating expense"])
    return rows


def _truth(cfg: GenConfig, panel: Panel) -> dict:
    teams = {}
    for tp in panel.teams:
        spec = tp.spec
        start = None if spec.rollout_month is None else panel.months.index(spec.rollout_month)
        teams[spec.team_id] = {
            "rollout_month": spec.rollout_month,
            "planted_effect_by_month": {} if start is None else {
                m: float(tp.effect[i]) for i, m in enumerate(panel.months) if i >= start
            },
            "curve": None if start is None else {"A": tp.curve_a, "k": tp.curve_k},
            "calibration_ref": spec.calibration_ref,
        }
    return {"seed": panel.seed, "generator_version": cfg.generator_version, "teams": teams}


SPEND_HEADER = ["usage_date", "team_id", "project_id", "user_id", "provider", "model",
                "input_tokens", "output_tokens", "cost_cents"]
GL_HEADER = ["entry_id", "period", "account_code", "cost_center", "debit_cents", "credit_cents", "memo"]
ROSTER_HEADER = ["employee_id", "team_id", "start_date", "end_date", "monthly_payroll_cents"]
OUTPUT_HEADER = ["team_id", "month", "metric_id", "value"]


def _output_rows(panel: Panel, months: list[str]) -> list[list]:
    return [[tp.spec.team_id, f"{m}-01", tp.spec.metric_id, f"{tp.output[i]:.6f}"]
            for tp in panel.teams for i, m in enumerate(panel.months) if m in months]


def write_incoming(cfg: GenConfig, panel: Panel, work: WorkDir) -> None:
    """Write the held-back month to `data/incoming/<month>/` and refresh `data/MANIFEST.incoming.sha256`.

    The month's rows only. The roster file holds the inserts for starts in the month and the end dates
    set in it, never an edit of an earlier row; the generated companies have a constant headcount, so
    it is empty here. `dim_team` does not change and has no incoming file.
    """
    months = [cfg.incoming_month]
    out = work.data / manifest.INCOMING_DIR / cfg.incoming_month
    out.mkdir(parents=True, exist_ok=True)
    _write_csv(out / "raw_ai_spend.csv", SPEND_HEADER, _spend_rows(cfg, panel, months))
    _write_csv(out / "raw_gl_journal.csv", GL_HEADER, _gl_rows(cfg, panel, months))
    _write_csv(out / "raw_hr_roster.csv", ROSTER_HEADER, [])
    _write_csv(out / "raw_output.csv", OUTPUT_HEADER, _output_rows(panel, months))
    manifest.write_incoming(work.data)


def write(cfg: GenConfig, panel: Panel, work: WorkDir) -> None:
    """Write the books for first_month..last_month, the held-back month, and the truth for the whole panel."""
    months = [m for m in panel.months if m <= cfg.last_month]
    work.books.mkdir(parents=True, exist_ok=True)
    work.truth.mkdir(parents=True, exist_ok=True)

    _write_csv(work.books / "raw_ai_spend.csv", SPEND_HEADER, _spend_rows(cfg, panel, months))
    _write_csv(work.books / "raw_gl_journal.csv", GL_HEADER, _gl_rows(cfg, panel, months))
    start = month_start(cfg.first_month).replace(year=month_start(cfg.first_month).year - 1).isoformat()
    _write_csv(work.books / "raw_hr_roster.csv",
               ROSTER_HEADER,
               [[f"{tp.spec.team_id}-e{i + 1:03d}", tp.spec.team_id, start, "",
                 int(round(tp.spec.monthly_payroll_per_head_usd * 100))]
                for tp in panel.teams for i in range(tp.spec.headcount)])
    _write_csv(work.books / "raw_output.csv", OUTPUT_HEADER, _output_rows(panel, months))
    _write_csv(work.books / "dim_team.csv", ["team_id", "name", "metric_id", "unit_label", "direction", "kind"],
               [[tp.spec.team_id, tp.spec.name, tp.spec.metric_id, tp.spec.unit_label, tp.spec.direction,
                 tp.spec.kind] for tp in panel.teams])
    write_incoming(cfg, panel, work)
    manifest.write_books(work.data)
    (work.truth / "truth.json").write_text(
        json.dumps(_truth(cfg, panel), indent=2, sort_keys=True) + "\n", encoding="utf-8")
