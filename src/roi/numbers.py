"""Every number the memo prints, enumerated in one place.

`enumerate_numbers` lists the numbers of a month's memo in a fixed order: the headline spend,
the payroll shares, the effects and their intervals, the return figures, the sensitivity grid,
the allocation, the agenda, the value-per-unit assumptions taken from the config file and the published
benchmark figures. The month's unassigned spend (the bucket with no owning team) follows the headline when
the books hold one; the agenda's within-user correlation and any configured sigma override come from the config file. Each
one names the model and the source-row predicate (bounded at the month) that back it and carries
the text to print. The build step that stores provenance and the memo that prints the numbers
both call it, so the two cannot disagree about which numbers exist or how they read.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import duckdb

from roi import alloc, benchmark, prov
from roi import roi as roi_calc
from roi.steps.provenance import SPEND_NAME
from roi.workdir import RoiError

UNASSIGNED = "unassigned"


@dataclass(frozen=True)
class Number:
    """A number to print: what it is called, the memo section it belongs to, its source and its text."""

    name: str
    section: str
    source_model: str
    source_predicate: str
    display_value: str
    value: float


def usd(cents: int) -> str:
    return f"{'-' if cents < 0 else ''}${abs(cents) // 100:,}.{abs(cents) % 100:02d}"


def usd_unit(v: float) -> str:
    return f"${v:,.2f}" if v >= 1 else f"${v:.4f}"


def _q(value: str) -> str:
    return value.replace("'", "''")


def has_table(con: duckdb.DuckDBPyConnection, table: str) -> bool:
    return bool(con.execute(
        "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'main' AND table_name = ?", [table]
    ).fetchone()[0])


def by_name(numbers: list[Number]) -> dict[str, Number]:
    return {n.name: n for n in numbers}


def enumerate_numbers(con: duckdb.DuckDBPyConnection, through: str, config: Path | None) -> list[Number]:
    """The numbers of the memo for `through` (`YYYY-MM`), in the order the memo prints them."""
    at = f"through_month = '{through}'"
    out: list[Number] = []

    def add(section: str, name: str, model: str, predicate: str, display: str, value: float) -> None:
        out.append(Number(name, section, model, predicate, display, value))

    spend_where = f"month = DATE '{through}-01'"
    cents = con.execute(f"SELECT sum(spend_cents) FROM fct_team_month WHERE {spend_where}").fetchone()[0]
    if cents is None:
        raise RoiError(2, f"no results for month {through}")
    add("headline", SPEND_NAME, "fct_team_month", spend_where, f"${cents // 100:,}.{cents % 100:02d}", cents / 100)

    # Absent when the books hold no unassigned bucket for the month, so a company without one prints no such line.
    if has_table(con, "fct_ai_spend_team_month"):
        unassigned = con.execute(
            f"SELECT sum(spend_cents)::BIGINT FROM fct_ai_spend_team_month WHERE {spend_where} AND team_id = '{UNASSIGNED}'"
        ).fetchone()[0]
        total = con.execute(f"SELECT sum(spend_cents)::BIGINT FROM fct_ai_spend_team_month WHERE {spend_where}").fetchone()[0]
        if unassigned is not None and total:
            add("headline", "unassigned_spend", "fct_ai_spend_team_month", f"{spend_where} AND team_id = '{UNASSIGNED}'",
                usd(unassigned), unassigned / 100)
            # The share rests on the whole month, since its denominator is every bucket's spend.
            add("headline", "unassigned_share", "fct_ai_spend_team_month", spend_where,
                f"{unassigned / total * 100:.1f}%", unassigned / total)

    roi_rows = {}
    if has_table(con, "roi_team"):
        for team, spend, benefit, mult, lo, hi in con.execute(
            f"SELECT team_id, spend_cents, benefit_cents, roi_multiple, roi_lo, roi_hi FROM roi_team WHERE {at}"
        ).fetchall():
            roi_rows[team] = (spend, benefit, mult, lo, hi)
    share_rows = {}
    if has_table(con, "fct_ai_share_of_payroll"):
        share_rows = dict(con.execute(
            "SELECT team_id, share FROM fct_ai_share_of_payroll WHERE month = ?", [f"{through}-01"]
        ).fetchall())

    def add_share(team: str | None) -> None:
        if team not in share_rows or share_rows[team] is None:
            return
        who = "company" if team is None else team
        where = "team_id IS NULL" if team is None else f"team_id = '{_q(team)}'"
        share = float(share_rows[team])
        add("return", f"payroll_share:{who}", "fct_ai_share_of_payroll", f"month = DATE '{through}-01' AND {where}",
            f"{share * 100:.2f}%", share)

    teams = con.execute(
        "SELECT t.team_id, t.name, e.status, e.effect, e.ci_lo, e.ci_hi"
        f" FROM est_team_effect e JOIN dim_team t USING (team_id) WHERE e.{at} ORDER BY t.name"
    ).fetchall()
    measured = []
    unmeasured = []
    for team, _, status, effect, lo, hi in teams:
        add_share(team)
        if status == "unmeasured":
            unmeasured.append(team)
        if status != "measured":
            continue
        measured.append(team)
        where = f"{at} AND team_id = '{_q(team)}'"
        for label, v in (("effect", effect), ("effect_lo", lo), ("effect_hi", hi)):
            add("return", f"{label}:{team}", "est_team_effect", where, f"{v:.3f}", v)
        if team in roi_rows:
            spend, benefit, mult, rlo, rhi = roi_rows[team]
            add("return", f"spend:{team}", "roi_team", where, usd(spend), spend / 100)
            add("return", f"benefit:{team}", "roi_team", where, usd(benefit), benefit / 100)
            for label, v in (("roi", mult), ("roi_lo", rlo), ("roi_hi", rhi)):
                if v is not None:
                    add("return", f"{label}:{team}", "roi_team", where, f"{v:.2f}", v)
    add_share(None)

    if has_table(con, "roi_sensitivity") and roi_rows:
        grid = con.execute(
            f"SELECT team_id, value_multiplier, roi_multiple, roi_survives_share FROM roi_sensitivity WHERE {at}"
            " ORDER BY team_id, value_multiplier"
        ).fetchall()
        multipliers = sorted({m for _, m, _, _ in grid})
        names = {team: name for team, name, *_ in teams}
        cells: dict[str, dict] = {}
        for team, m, mult, share in grid:
            cells.setdefault(team, {"cells": {}, "share": share})["cells"][m] = mult
        for m in multipliers:
            add("return", f"multiplier:{m!r}", "roi_sensitivity", f"{at} AND value_multiplier = {m!r}", f"{m:g}", m)
        for team in sorted(cells, key=lambda t: names.get(t, t)):
            entry = cells[team]
            where = f"{at} AND team_id = '{_q(team)}'"
            for m in multipliers:
                if entry["cells"].get(m) is not None:
                    add("return", f"sensitivity:{team}:{m!r}", "roi_sensitivity", f"{where} AND value_multiplier = {m!r}",
                        f"{entry['cells'][m]:.2f}", entry["cells"][m])
            if entry["share"] is not None:
                add("return", f"survives:{team}", "roi_sensitivity", where, f"{entry['share'] * 100:.0f}%", entry["share"])

    if has_table(con, "alloc_result"):
        rows = con.execute(
            "SELECT a.team_id, a.budget_cents, a.amount_cents, a.funded_share_in_draws, a.beyond_observed_share"
            f" FROM alloc_result a JOIN dim_team t USING (team_id) WHERE a.{at} ORDER BY t.name"
        ).fetchall()
        if rows:
            add("allocation", "allocation_budget", "alloc_result", at, usd(rows[0][1]), rows[0][1] / 100)
            for team, _, amount, funded, beyond in rows:
                where = f"{at} AND team_id = '{_q(team)}'"
                add("allocation", f"allocation:{team}", "alloc_result", where, usd(amount), amount / 100)
                add("allocation", f"funded_share:{team}", "alloc_result", where, f"{funded * 100:.0f}%", funded)
                if beyond is not None:
                    add("allocation", f"beyond_observed_share:{team}", "alloc_result", where, f"{beyond * 100:.0f}%", beyond)

    if has_table(con, "agenda_item"):
        items = {r[0]: r for r in con.execute(
            f"SELECT team_id, design, n_per_arm, length_months, delta FROM agenda_item WHERE {at}"
        ).fetchall()}
        assumed = [team for team in unmeasured if team in items]
        if assumed and config is not None:
            alloc_cfg = alloc.load_config(config)
            if alloc_cfg is not None:
                predicate = prov.config_predicate(config.name, "rho")
                if prov.config_rows(predicate, config)[1]:
                    add("agenda", "rho", prov.CONFIG_MODEL, predicate, f"{alloc_cfg.rho:g}", alloc_cfg.rho)
                for team in assumed:
                    if team in alloc_cfg.sigma_override:
                        sigma = alloc_cfg.sigma_override[team]
                        add("agenda", f"sigma_override:{team}", prov.CONFIG_MODEL,
                            prov.config_predicate(config.name, f"sigma_override.{team}"), f"{sigma:g}", sigma)
        for team in unmeasured:
            if team not in items:
                continue
            _, _, n, length, delta = items[team]
            where = f"{at} AND team_id = '{_q(team)}'"
            for label, v, text in (
                ("agenda_n_per_arm", n, f"{n:,}" if n is not None else ""),
                ("agenda_length_months", length, f"{length:,}" if length is not None else ""),
                ("agenda_delta", delta, f"{delta:.3f}" if delta is not None else ""),
            ):
                if v is not None:
                    add("agenda", f"{label}:{team}", "agenda_item", where, text, v)

    if config is not None:
        cfg = roi_calc.load_config(config)
        for team in measured:
            tc = cfg.teams.get(team)
            if tc is None or tc.value is None:
                continue
            for part in ("low", "base", "high"):
                v = getattr(tc.value, part)
                add("assumptions", f"value_{part}:{team}", prov.CONFIG_MODEL,
                    prov.config_predicate(config.name, f"value_per_unit.{team}.{part}"), usd_unit(v), v)
    out += benchmark.enumerate_numbers(con, through)
    return out
