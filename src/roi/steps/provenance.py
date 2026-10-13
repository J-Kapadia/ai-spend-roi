"""Write the provenance of the memo's one number: `prov_record` and `prov_build` (plan D-17)."""

from __future__ import annotations

import subprocess
import uuid
from datetime import datetime, timezone

import duckdb

from roi import prov
from roi.steps import BuildContext

NAME = "provenance"
ORDER = 40

SPEND_NAME = "company_ai_spend_usd"


def _git_sha() -> str:
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        return "no-git"
    return out.stdout.strip() or "no-git"


def run(ctx: BuildContext) -> None:
    con = duckdb.connect(str(ctx.db))
    try:
        predicate = f"month = DATE '{ctx.through}-01'"
        _, rows = prov.source_rows(con, "fct_team_month", predicate)
        cents = con.execute(f"SELECT sum(spend_cents) FROM fct_team_month WHERE {predicate}").fetchone()[0]
        display = f"${cents // 100:,}.{cents % 100:02d}"
        prov_id = prov.make_id(SPEND_NAME, ctx.through, "fct_team_month", predicate, display)

        con.execute("BEGIN")
        con.execute(prov.PROV_RECORD_DDL)
        con.execute(prov.PROV_BUILD_DDL)
        con.execute(
            "INSERT INTO prov_record VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [prov_id, SPEND_NAME, f"{ctx.through}-01", display, cents / 100, "fct_team_month", predicate,
             len(rows), prov.checksum(rows), NAME],
        )
        con.execute(
            "INSERT INTO prov_build VALUES (?, ?, ?, ?, ?, ?, ?)",
            [prov_id, uuid.uuid4().hex[:12], _git_sha(), "script", "roi build", "dbt build",
             datetime.now(timezone.utc).replace(tzinfo=None)],
        )
        con.execute("COMMIT")
    finally:
        con.close()
