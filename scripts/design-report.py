#!/usr/bin/env python3
"""Render the design numbers of the generator config as Markdown tables.

    uv run python scripts/design-report.py [--config config/gen.yaml] [--roi config/roi.yaml] [--out PATH]

The report holds, from `roi.design`: for each of the last month written to the books and the
held-back month, a table per team of its months before and after rollout, controls per cell,
planted effect, analytic standard error, degrees of freedom, t multiplier and planted effect
over standard error; the noise solution (every pass of the fixed-point iteration, the converged
sds, the median rule and the sds in the config beside them); the delta-method standard error of
the marginal return of each measured team's curve; the value per unit of each team from the roi
config, with an empty column for the reasoning; and the check of `memo_budget_usd` against its
formula. It prints to stdout, or writes `--out`, so the note can be produced again after any
config change.

Exit 0; exit 2 when a config file cannot be read or the arguments are wrong.
"""

from __future__ import annotations

import argparse
import dataclasses
import math
import statistics
import sys
from pathlib import Path

import yaml

from roi import design
from roi.gen import config as gen_config
from roi.gen.config import GenConfig
from roi.workdir import RoiError

ROOT = Path(__file__).resolve().parent.parent
TARGET = 3.0  # planted effect over standard error that the noise is solved for
TOL = 1e-6
MAX_PASSES = 200
BUDGET_SHARE = 0.4  # of the summed headroom of the measured teams
BUDGET_CAP_MULTIPLE = 1.5  # the spend cap, as a multiple of the target spend per head
BUDGET_ROUND_USD = 1000


def table(header: list[str], rows: list[list[str]]) -> list[str]:
    out = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    out += ["| " + " | ".join(row) + " |" for row in rows]
    return out


def with_noise(cfg: GenConfig, sds: dict[str, float]) -> GenConfig:
    teams = tuple(dataclasses.replace(s, noise_sd=sds[s.team_id]) for s in cfg.teams)
    return dataclasses.replace(cfg, teams=teams)


def through_of(cfg: GenConfig, team: str) -> str:
    return cfg.incoming_month if team in design.MEASURED_AT_INCOMING else cfg.last_month


def design_tables(cfg: GenConfig) -> list[str]:
    out: list[str] = []
    for through in (cfg.last_month, cfg.incoming_month):
        designs = design.report(cfg, through)
        out += [f"## Teams measured through {through}", ""]
        rows = [
            [
                d.team_id, str(d.n_pre), str(d.n_post), "/".join(str(n) for n in d.controls_per_cell),
                f"{d.planted:.4f}", f"{d.se:.4f}", str(d.df), f"{d.t_mult:.3f}", f"{d.ratio:.2f}",
            ]
            for d in designs.values()
        ]
        header = ["team", "n_pre", "n_post", "controls per cell", "planted effect", "analytic SE", "df", "t multiplier", "planted / SE"]
        out += table(header, rows)
        skipped = [s.team_id for s in cfg.teams if s.team_id not in designs]
        out += ["", "Planted effect and SE are in log points, averaged over the post months that have a control.",
                f"Not measurable through {through}: {', '.join(sorted(skipped)) or 'none'}.", ""]
    return out


def noise_passes(cfg: GenConfig) -> tuple[list[tuple[int, dict[str, float], float]], dict[str, float]]:
    """The passes of the fixed-point iteration of `design.solve_noise`, each with the sds it
    produced and the largest relative change, and the planted effect each solved team is held to."""
    targets = [(t, cfg.last_month) for t in design.MEASURED_AT_LAST] + [(t, cfg.incoming_month) for t in design.MEASURED_AT_INCOMING]
    solved = [t for t, _ in targets]
    planted = {t: design.planted_team_effect(cfg, t, through) for t, through in targets}
    sds = {s.team_id: s.noise_sd for s in cfg.teams}
    passes: list[tuple[int, dict[str, float], float]] = [(0, {t: sds[t] for t in solved}, math.nan)]
    for n in range(1, MAX_PASSES + 1):
        median = statistics.median(sds[t] for t in design.MEASURED_AT_LAST)
        prev = {**sds, **{s.team_id: median for s in cfg.teams if s.team_id not in solved}}
        nxt = dict(prev)
        for team, through in targets:
            zero = design.team_effect_se(with_noise(cfg, {**prev, team: 0.0}), team, through) ** 2
            one = design.team_effect_se(with_noise(cfg, {**prev, team: 1.0}), team, through) ** 2
            room = (planted[team] / TARGET) ** 2 - zero
            nxt[team] = max(design.NOISE_FLOOR, math.sqrt(max(room, 0.0) / (one - zero)))
        change = max(abs(nxt[t] - prev[t]) / prev[t] for t in solved)
        sds = nxt
        passes.append((n, {t: sds[t] for t in solved}, change))
        if change < TOL:
            return passes, planted
    raise RuntimeError(f"the noise iteration did not converge in {MAX_PASSES} passes")


def noise_section(cfg: GenConfig) -> list[str]:
    passes, planted = noise_passes(cfg)
    converged = design.solve_noise(cfg, target=TARGET, tol=TOL, max_iter=MAX_PASSES)
    solved = list(passes[0][1])
    last = passes[-1][1]
    if any(not math.isclose(last[t], converged[t], rel_tol=1e-9) for t in solved):
        raise RuntimeError("the iteration shown here disagrees with design.solve_noise")
    out = ["## Noise solution", "",
           f"The noise sd of each team (log points) is set so that planted effect / analytic SE is {TARGET:g} "
           f"for the {len(design.MEASURED_AT_LAST)} teams measured through {cfg.last_month} and for "
           f"{', '.join(design.MEASURED_AT_INCOMING)} through {cfg.incoming_month}.", "",
           "### Inputs", ""]
    out += table(["team", "through", "planted effect", "target SE (planted / target)"],
                 [[t, through_of(cfg, t), f"{planted[t]:.4f}", f"{planted[t] / TARGET:.4f}"] for t in solved])
    out += ["", "### Iteration", "",
            "Pass 0 is the sd in the config. Each pass solves every team's own sd given the others' sds "
            "from the previous pass, the other teams (the control and the one not measured) taking the "
            "median of the first five; it stops when the largest relative change is below "
            f"{TOL:g}.", ""]
    out += table(["pass", *solved, "max relative change"],
                 [[str(n), *[f"{sds[t]:.6f}" for t in solved], "" if math.isnan(change) else f"{change:.2e}"]
                  for n, sds, change in passes])
    median = statistics.median(converged[t] for t in design.MEASURED_AT_LAST)
    out += ["", "### Converged values", "",
            f"Median rule: the teams not solved take the median of the {len(design.MEASURED_AT_LAST)} teams "
            f"measured through {cfg.last_month}, here {median:.6f}.", ""]
    rows = []
    for s in cfg.teams:
        rule = "solved" if s.team_id in solved else "median"
        diff = (s.noise_sd - converged[s.team_id]) / converged[s.team_id]
        rows.append([s.team_id, rule, f"{converged[s.team_id]:.6f}", f"{s.noise_sd:.6f}", f"{diff:+.2%}"])
    out += table(["team", "rule", "converged sd", "sd in config", "relative difference"], rows)
    return out + [""]


def marginal_section(cfg: GenConfig) -> list[str]:
    rows = []
    for team in (*design.MEASURED_AT_LAST, *design.MEASURED_AT_INCOMING):
        through = through_of(cfg, team)
        try:
            se = design.marginal_se(cfg, team, through)
            flag = "yes" if design.identified_at_design(cfg, team, through) else "no"
        except ValueError:
            continue
        rows.append([team, through, f"{se:.4g}", flag])
    out = ["## Dose-response curve at design", "",
           "`marginal SE` is the delta-method standard error of the marginal return at the current spend "
           "(log points per cent of monthly spend per head). A team is identified at design when its "
           "planted marginal return exceeds its one-sided 95% bound.", ""]
    return out + table(["team", "through", "marginal SE", "identified at design"], rows) + [""]


def value_section(cfg: GenConfig, roi: dict) -> list[str]:
    values = roi.get("value_per_unit") or {}
    rows = []
    for s in cfg.teams:
        v = values.get(s.team_id) or {}
        cell = lambda k: "" if v.get(k) is None else f"{v[k]:g}"  # noqa: E731
        rows.append([s.team_id, str(v.get("kind", s.kind)), s.unit_label, cell("low"), cell("base"), cell("high"), ""])
    out = ["## Value per unit", "",
           "USD per unit of output, from the roi config. The reasoning column is left for the author.", ""]
    return out + table(["team", "kind", "unit", "low", "base", "high", "reasoning"], rows) + [""]


def budget_section(cfg: GenConfig, roi: dict) -> list[str]:
    rows, headroom = [], 0.0
    for team in design.MEASURED_AT_LAST:
        s = design._spec(cfg, team)
        h = s.headcount * (BUDGET_CAP_MULTIPLE * s.target_spend_per_head_usd - s.target_spend_per_head_usd)
        headroom += h
        rows.append([team, str(s.headcount), f"{s.target_spend_per_head_usd:g}", f"{h:,.2f}"])
    raw = BUDGET_SHARE * headroom
    formula = int(math.floor(raw / BUDGET_ROUND_USD + 0.5)) * BUDGET_ROUND_USD
    configured = roi.get("memo_budget_usd")
    verdict = "matches the formula" if configured == formula else "DIFFERS from the formula"
    out = ["## Memo budget", "",
           f"`memo_budget_usd` is {BUDGET_SHARE:.0%} of the summed headroom, headcount × ({BUDGET_CAP_MULTIPLE:g} × target − target) "
           f"in USD per month over the {len(design.MEASURED_AT_LAST)} teams measured through {cfg.last_month}, "
           f"rounded to the nearest {BUDGET_ROUND_USD:,}.", ""]
    out += table(["team", "headcount", "target spend per head", "headroom"], rows)
    out += ["", f"- summed headroom: {headroom:,.2f}",
            f"- {BUDGET_SHARE:g} × summed headroom: {raw:,.2f}",
            f"- rounded: {formula:,}",
            f"- `memo_budget_usd` in the roi config: {configured if configured is not None else 'not set'} ({verdict})", ""]
    return out


def read_yaml(path: Path, what: str) -> dict:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as e:
        raise RoiError(2, f"cannot read {what} {path}: {e.strerror}") from e
    except yaml.YAMLError as e:
        raise RoiError(2, f"{what} {path} is not valid YAML: {e}") from e
    if not isinstance(raw, dict):
        raise RoiError(2, f"{what} {path} is not a YAML mapping")
    return raw


def shown(path: Path) -> str:
    """The path as the report names it: relative to the repository when it is inside, so the
    report does not change with where the repository is checked out."""
    try:
        return str(path.resolve().relative_to(ROOT))
    except ValueError:
        return str(path)


def build(config_path: Path, roi_path: Path) -> str:
    cfg = gen_config.load(config_path)
    roi = read_yaml(roi_path, "roi config")
    lines = ["# Design report", "",
             f"Generator config `{shown(config_path)}` (seed {cfg.seed}), roi config `{shown(roi_path)}`. "
             f"Panel {cfg.first_month} to {cfg.incoming_month}; {cfg.last_month} is the last month in the books.", ""]
    lines += design_tables(cfg) + noise_section(cfg) + marginal_section(cfg) + value_section(cfg, roi) + budget_section(cfg, roi)
    return "\n".join(lines).rstrip("\n") + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="design-report.py", description=__doc__.split("\n\n")[0])
    parser.add_argument("--config", type=Path, default=ROOT / "config" / "gen.yaml", help="generator config (default: config/gen.yaml)")
    parser.add_argument("--roi", type=Path, default=ROOT / "config" / "roi.yaml", help="roi config (default: config/roi.yaml)")
    parser.add_argument("--out", type=Path, help="write the report here instead of stdout")
    args = parser.parse_args(argv)
    try:
        text = build(args.config, args.roi)
        if args.out is None:
            sys.stdout.write(text)
        else:
            args.out.write_text(text, encoding="utf-8")
    except RoiError as e:
        print(f"error[{e.exit_code}]: {e.message}", file=sys.stderr)
        return e.exit_code
    except OSError as e:
        print(f"error[2]: cannot write {args.out}: {e.strerror}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
