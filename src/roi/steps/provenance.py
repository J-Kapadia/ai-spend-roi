"""Write the provenance of every number of the memo: `prov_record` and `prov_build`.

A record is immutable and part of the warehouse content hash; a build row says which commit and
which producer wrote it and is not hashed, so a rebuild at another commit leaves the records alone.
"""

from __future__ import annotations

import subprocess
import uuid
from datetime import datetime, timezone

from roi import prov, steps
from roi.db import connect as db_connect
from roi.steps import BuildContext
from roi.steps import roi as roi_step

NAME = "provenance"
ORDER = 90  # after every step whose result tables hold a number of the memo

SPEND_NAME = "company_ai_spend_usd"
CHECKS_PASSED = "dbt build, gl tie"
STAGING_DB = "roi.staging.duckdb"


def _git(*args: str) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(["git", *args], capture_output=True, text=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        return None


def _git_sha() -> str:
    """The short HEAD of the current directory, `-dirty` when the tree has uncommitted changes."""
    head = _git("rev-parse", "--short", "HEAD")
    if head is None or not head.stdout.strip():
        return "no-git"
    status = _git("status", "--porcelain")
    return head.stdout.strip() + ("-dirty" if status is not None and status.stdout.strip() else "")


def run(ctx: BuildContext) -> None:
    from roi.numbers import enumerate_numbers  # numbers imports SPEND_NAME from this module

    config = roi_step.config_path(ctx.work)
    sha = _git_sha()
    # A roll-forward is the only producer that works on the staging file; this tells the two apart.
    producer = "roi rollforward" if ctx.db.name == STAGING_DB else "roi build"
    con = db_connect(ctx.db)
    try:
        numbers = enumerate_numbers(con, ctx.through, config if config.exists() else None)
        con.execute("BEGIN")
        steps.create_table(con, prov.PROV_RECORD_DDL)
        steps.create_table(con, prov.PROV_BUILD_DDL)
        build_id = uuid.uuid4().hex[:12]
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        for n in numbers:
            prov_id = prov.make_id(n.name, ctx.through, n.source_model, n.source_predicate, n.display_value)
            if con.execute("SELECT 1 FROM prov_record WHERE prov_id = ?", [prov_id]).fetchone():
                continue
            _, rows = prov.rows_of(con, n.source_model, n.source_predicate, config)
            con.execute(
                "INSERT INTO prov_record VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [prov_id, n.name, f"{ctx.through}-01", n.display_value, n.value, n.source_model,
                 n.source_predicate, len(rows), prov.checksum(rows), NAME],
            )
            con.execute(
                "INSERT INTO prov_build VALUES (?, ?, ?, ?, ?, ?, ?)",
                [prov_id, build_id, sha, "script", producer, CHECKS_PASSED, now],
            )
        con.execute("COMMIT")
    finally:
        con.close()
