"""The agenda has one item for each unmeasured team and none for any other."""

from __future__ import annotations

import duckdb

from roi import estimator, verify_books
from roi.checks import CheckResult
from roi.workdir import WorkDir

NAME = "agenda_items"
ORDER = 40
TABLE = "agenda_item"


def run(con: duckdb.DuckDBPyConnection, through: str, work: WorkDir) -> CheckResult:
    verify_books.require(con, TABLE)
    est = verify_books.team_effects(con, through)
    unmeasured = verify_books.teams_with_status(est, estimator.UNMEASURED)
    have = verify_books.teams_in(con, TABLE, through)
    if not have and not (work.root / "config" / "roi.yaml").exists():
        # the build had no config to write an agenda from
        return CheckResult(NAME, note=f"written empty, no config/roi.yaml in the work dir: {TABLE}")
    failures = [f"team={team} is unmeasured and has no item in {TABLE}" for team in sorted(unmeasured - have)]
    failures += [
        f"team={team} is {est.get(team, {}).get('status', 'unknown')} and has an item in {TABLE}"
        for team in sorted(have - unmeasured)
    ]
    return CheckResult(NAME, tuple(failures))
