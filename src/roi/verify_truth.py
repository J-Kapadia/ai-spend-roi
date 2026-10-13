"""Verification group (b): the planted team effect lies inside the 95% interval.

This is the only module that reads the truth file, and it is reachable only from the
`roi verify` command and from tests; no pipeline step, command or other module imports it.

The estimand is the equal-weight mean of the planted effects over the months of the team's
group-time cells (the months its team effect averages), which for a team with a comparison in
every post month is its post-rollout months up to the through month. Planted effects are in log
points and signed by direction, the scale of the estimate.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import duckdb

from roi.workdir import RoiError, WorkDir


@dataclass(frozen=True)
class Coverage:
    team_id: str
    effect: float
    ci_lo: float
    ci_hi: float
    planted: float

    @property
    def inside(self) -> bool:
        return self.ci_lo <= self.planted <= self.ci_hi


@dataclass(frozen=True)
class CoverageReport:
    through: str
    teams: tuple[Coverage, ...]

    @property
    def failures(self) -> list[str]:
        return [
            f"interval coverage failed: team={c.team_id} planted={c.planted:.6f} "
            f"interval=[{c.ci_lo:.6f}, {c.ci_hi:.6f}] effect={c.effect:.6f}"
            for c in self.teams if not c.inside
        ]


def _load_truth(work: WorkDir) -> dict:
    path = work.truth / "truth.json"
    try:
        truth = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise RoiError(4, f"the truth file is missing at {path}; run `roi gen`") from None
    except (OSError, ValueError) as e:
        raise RoiError(4, f"the truth file {path} cannot be read: {e}") from None
    if not isinstance(truth.get("teams"), dict):
        raise RoiError(4, f"the truth file {path} has no `teams` table")
    return truth


def check_interval_coverage(con: duckdb.DuckDBPyConnection, work: WorkDir, through: str) -> CoverageReport:
    """Group (b). Raises RoiError(4, ...) when the truth file is missing or does not cover a measured
    team, and RoiError(3, ...) when a measured team has no cells; the caller turns a team outside
    its interval into exit 1."""
    truth = _load_truth(work)
    measured = con.execute(
        "SELECT team_id, effect, ci_lo, ci_hi FROM est_team_effect"
        " WHERE through_month = ? AND status = 'measured' ORDER BY team_id", [through],
    ).fetchall()
    rows = []
    for team, effect, ci_lo, ci_hi in measured:
        months = [m for (m,) in con.execute(
            "SELECT strftime(t, '%Y-%m') FROM est_att_gt WHERE through_month = ? AND team_id = ? ORDER BY t",
            [through, team],
        ).fetchall()]
        planted_by_month = {
            str(m)[:7]: v for m, v in (truth["teams"].get(team) or {}).get("planted_effect_by_month", {}).items()
        }
        if not months:
            raise RoiError(3, f"team={team} is measured and has no group-time cells in est_att_gt")
        absent = [m for m in months if m not in planted_by_month]
        if absent:
            raise RoiError(4, f"the truth file has no planted effect for team={team} month={absent[0]}")
        rows.append(Coverage(team, effect, ci_lo, ci_hi, sum(planted_by_month[m] for m in months) / len(months)))
    return CoverageReport(through, tuple(rows))
