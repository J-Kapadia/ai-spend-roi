"""Write the provenance of every number of the memo: `prov_record` and `prov_build`.

A record is immutable and part of the warehouse content hash; a build row says which commit and
which producer wrote it and is not hashed, so a rebuild at another commit leaves the records alone.
"""

from __future__ import annotations

import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path

from roi import prov, steps
from roi.db import connect as db_connect
from roi.steps import BuildContext
from roi.steps import roi as roi_step

NAME = "provenance"
ORDER = 90  # after every step whose result tables hold a number of the memo

SPEND_NAME = "company_ai_spend_usd"
CHECKS_PASSED = "dbt build, gl tie"
STAGING_DB = "roi.staging.duckdb"


PACKAGE_ROOT = Path(__file__).resolve().parents[3]
SHA_FILE = ".gitsha"
UNKNOWN = "unknown"


def _git(root: Path, *args: str) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        return None


def _has_git(root: Path) -> bool:
    """True when `root` itself carries .git (a directory, or the file of a linked work tree). Git is
    never allowed to walk up from a tree without one: that would read an enclosing repository."""
    return (root / ".git").exists()


def _archived_sha() -> str:
    """The commit `git archive` substituted into the tracked sha file, `unknown` when it was not."""
    try:
        text = (PACKAGE_ROOT / SHA_FILE).read_text().strip()
    except OSError:
        return UNKNOWN
    return text if text and "$" not in text else UNKNOWN


def _git_sha(work: Path) -> str:
    """The short HEAD of the project's own repository, `-dirty` when its tree has uncommitted changes.

    The repository is the build's work directory, else the current directory, and only when that
    directory has .git at its own top. A tree without one (a `git archive` extraction, possibly sitting
    inside another repository) is identified by its substituted sha file or `unknown`, never `-dirty`.
    """
    for root in (work, Path.cwd()):
        if _has_git(root):
            head = _git(root, "rev-parse", "--short", "HEAD")
            if head is None or not head.stdout.strip():
                return UNKNOWN
            status = _git(root, "status", "--porcelain")
            return head.stdout.strip() + ("-dirty" if status is not None and status.stdout.strip() else "")
    return _archived_sha()


def run(ctx: BuildContext) -> None:
    from roi.numbers import enumerate_numbers  # numbers imports SPEND_NAME from this module

    config = roi_step.config_path(ctx.work)
    sha = _git_sha(ctx.work.root)
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
