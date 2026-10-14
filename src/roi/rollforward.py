"""Roll the published warehouse forward by one month, and publish a warehouse file safely.

`roi rollforward --month M` copies `roi.duckdb` to `roi.staging.duckdb`, appends the files of
`data/incoming/M/` to the `raw_*` tables, rebuilds the models with their tests, checks the ledger
tie, writes the results for M next to those of the earlier months, and only then swaps the file in.
The published file is never written in place, so a failure at any point leaves it as it was.

Publishing is a fixed sequence (a build runs steps 2 to 4, a roll-forward all four):

1. hard-link `roi.duckdb` to `roi.prev.duckdb` (so `roi.duckdb` is never absent);
2. write `.complete.next` with the content hash of the new file;
3. `os.replace` the new file onto `roi.duckdb`;
4. `os.replace` `.complete.next` onto `.complete`.

A process killed between 3 and 4 leaves `.complete` behind the file. `promote_pending` finishes the
swap when `.complete.next` equals the content hash of `roi.duckdb`, and drops it otherwise.

This module reads no truth: it imports `roi.verify_books` (the ledger tie) and never `roi.verify_truth`.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import re
import shutil
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterator

import click
import duckdb

from roi.db import connect as db_connect
from roi import steps, verify_books
from roi.gen import manifest
from roi.steps import load
from roi.workdir import RoiError, WorkDir

MONTH = re.compile(r"\d{4}-(0[1-9]|1[0-2])")
# The one table the content hash leaves out: build facts carry a clock and a random id.
NOT_HASHED = ("prov_build",)


# ---- months ------------------------------------------------------------------------------------

def next_month(month: str) -> str:
    y, m = int(month[:4]), int(month[5:])
    return f"{y + 1:04d}-01" if m == 12 else f"{y:04d}-{m + 1:02d}"


def check_month(month: str) -> None:
    if not MONTH.fullmatch(month):
        raise RoiError(2, f"bad month {month!r}; expected YYYY-MM")


def last_month(con: duckdb.DuckDBPyConnection) -> str:
    """The last month with output in the warehouse."""
    (m,) = con.execute("SELECT max(strftime(month, '%Y-%m')) FROM raw_output").fetchone()
    if m is None:
        raise RoiError(3, "the warehouse holds no output; rebuild it")
    return m


def incoming_months(work: WorkDir) -> list[str]:
    root = work.data / manifest.INCOMING_DIR
    return sorted(p.name for p in root.iterdir() if p.is_dir() and MONTH.fullmatch(p.name)) if root.is_dir() else []


# ---- content hash ------------------------------------------------------------------------------

def _fmt(v: object) -> str:
    """A value in the fixed form of the content hash: integers as-is, floats via repr, dates ISO 8601."""
    if v is None:
        return ""
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    return repr(v) if isinstance(v, float) else str(v)


def content_hash(db: Path) -> str:
    """SHA-256 over every table but the build facts, in table-name order, rows ordered by all columns.

    A DuckDB file is not byte-reproducible, so two warehouses are compared by content, never by bytes.
    """
    con = db_connect(db, read_only=True)
    try:
        h = hashlib.sha256()
        tables = [t for (t,) in con.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'"
            " AND table_type = 'BASE TABLE' ORDER BY table_name"
        ).fetchall() if t not in NOT_HASHED]
        for table in tables:
            h.update(f"table {table}\n".encode())
            for row in con.execute(f'SELECT * FROM "{table}" ORDER BY ALL').fetchall():
                h.update(("|".join(_fmt(v) for v in row) + "\n").encode())
        return h.hexdigest()
    finally:
        con.close()


# ---- lock and marker ---------------------------------------------------------------------------

def lock_path(work: WorkDir) -> Path:
    return work.warehouse / ".lock"


def marker_path(work: WorkDir) -> Path:
    return work.warehouse / ".complete"


def marker_next_path(work: WorkDir) -> Path:
    return work.warehouse / ".complete.next"


@contextmanager
def locked(work: WorkDir) -> Iterator[None]:
    """Hold the advisory lock on the work dir's warehouse. A second process exits 2, "warehouse busy".
    The operating system drops the lock with the process, so a killed run never leaves it held."""
    work.warehouse.mkdir(parents=True, exist_ok=True)
    with lock_path(work).open("a") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RoiError(2, "warehouse busy") from None
        yield


def read_marker(work: WorkDir) -> str | None:
    path = marker_path(work)
    return path.read_text(encoding="utf-8").strip() if path.exists() else None


def promote_pending(work: WorkDir) -> bool:
    """Finish a swap that was killed between steps 3 and 4. Returns True when the marker was promoted.

    `.complete.next` equal to the content hash of the published file means the file swap happened:
    the marker is replaced. Anything else means the swap did not happen, and the leftover is dropped.
    The caller holds the lock.
    """
    pending = marker_next_path(work)
    if not pending.exists():
        return False
    want = pending.read_text(encoding="utf-8").strip()
    if work.warehouse_db.exists() and read_marker(work) != want and content_hash(work.warehouse_db) == want:
        os.replace(pending, marker_path(work))
        return True
    pending.unlink()
    return False


def recover(work: WorkDir) -> None:
    """For a command that only reads: promote a pending marker if no other process is writing."""
    if not marker_next_path(work).exists():
        return
    try:
        with locked(work):
            promote_pending(work)
    except RoiError:
        pass  # a writer holds the lock; it finishes the swap itself


def check_marker(work: WorkDir) -> str:
    """The content hash of the published file, which must equal `.complete`. Exit 3 when it does not."""
    got = content_hash(work.warehouse_db)
    want = read_marker(work)
    if want is None:
        raise RoiError(3, f"{marker_path(work)} is missing; rebuild the warehouse with `roi build`")
    if got != want:
        raise RoiError(3, f"{work.warehouse_db} does not match {marker_path(work)}; rebuild it with `roi build`")
    return got


def swap_marker(work: WorkDir) -> None:
    """Step 4 of the swap sequence."""
    os.replace(marker_next_path(work), marker_path(work))


def publish(work: WorkDir, staged: Path, *, keep_previous: bool) -> str:
    """Publish a fully checked file onto `roi.duckdb` by the swap sequence. Returns its content hash."""
    digest = content_hash(staged)
    if keep_previous and work.warehouse_db.exists():  # 1
        tmp = work.warehouse / "roi.prev.duckdb.tmp"
        tmp.unlink(missing_ok=True)
        os.link(work.warehouse_db, tmp)
        os.replace(tmp, work.warehouse / "roi.prev.duckdb")
    marker_next_path(work).write_text(digest + "\n", encoding="utf-8")  # 2
    os.replace(staged, work.warehouse_db)  # 3
    swap_marker(work)  # 4
    return digest


def remove_db(path: Path) -> None:
    path.unlink(missing_ok=True)
    path.with_name(path.name + ".wal").unlink(missing_ok=True)


# ---- the incoming month ------------------------------------------------------------------------

def incoming_files(work: WorkDir, month: str) -> dict[str, Path]:
    out = work.data / manifest.INCOMING_DIR / month
    files = {name: out / f"{name}.csv" for name in manifest.INCOMING_FILES}
    missing = [p.name for p in files.values() if not p.exists()]
    if missing:
        raise RoiError(2, f"no incoming data for {month}: missing {', '.join(missing)} in {out}")
    return files


def check_manifest(work: WorkDir, month: str) -> dict[str, str]:
    """Check the month's files against `data/MANIFEST.incoming.sha256`; returns each file's SHA-256."""
    path = work.data / manifest.INCOMING_NAME
    if not path.exists():
        raise RoiError(3, f"{path} is missing; the incoming month cannot be checked")
    pinned = manifest.parse(path.read_text(encoding="utf-8"))
    digests = {}
    for name, file in incoming_files(work, month).items():
        key = manifest.incoming_path(month, name)
        digests[name] = manifest.sha256_file(file)
        if key not in pinned:
            raise RoiError(3, f"{key} is not in {path.name}")
        if pinned[key] != digests[name]:
            raise RoiError(3, f"{key} does not match {path.name}: the file changed since the manifest was written")
    return digests


def ingested(con: duckdb.DuckDBPyConnection, month: str) -> dict[str, str]:
    """File name -> SHA-256 recorded for an ingested month; empty when the month was not ingested."""
    rows = con.execute(
        "SELECT file, sha256 FROM rollforward_ingest WHERE strftime(month, '%Y-%m') = ?", [month]
    ).fetchall()
    return dict(rows)


def check_expected(con: duckdb.DuckDBPyConnection, month: str) -> None:
    """Exit 2 unless `month` is the month after the last one in the warehouse."""
    last = last_month(con)
    if month != next_month(last):
        raise RoiError(2, f"expected {next_month(last)} after {last}, not {month}")


def ingest(work: WorkDir, db: Path, month: str, digests: dict[str, str]) -> None:
    """Append the month's files to the `raw_*` tables of `db` and record them in `rollforward_ingest`."""
    files = incoming_files(work, month)
    con = db_connect(db)
    try:
        con.execute("BEGIN")
        for name, file in files.items():
            con.execute(
                f"INSERT INTO {name} SELECT * FROM read_csv(?, header = true, columns = ?)",
                [str(file), load.TABLES[name]],
            )
        con.executemany(
            "INSERT INTO rollforward_ingest VALUES (?, ?, ?)",
            [(f"{month}-01", files[name].name, digests[name]) for name in files],
        )
        con.execute("COMMIT")
    finally:
        con.close()


def advance(work: WorkDir, db: Path, month: str) -> None:
    """Take the file `db` one month on: check the manifest, ingest, run dbt with its tests, check the
    ledger tie, then write the results for `month` beside those of the earlier months.

    Raises RoiError: 2 for a month out of order or files missing, 3 for a manifest, dbt or tie failure.
    """
    check_month(month)
    con = db_connect(db, read_only=True)
    try:
        check_expected(con, month)
    finally:
        con.close()
    digests = check_manifest(work, month)
    click.echo(f"ingest {month}")
    ingest(work, db, month, digests)
    ctx = steps.BuildContext(work=work, db=db, through=month)
    for step in steps.discover():
        if step.NAME == "load":
            continue
        click.echo(f"step {step.NAME}")
        step.run(ctx)
        if step.NAME == "dbt":
            con = db_connect(db, read_only=True)
            try:
                verify_books.check_gl_tie(con, month)
            finally:
                con.close()


# ---- roi rollforward ---------------------------------------------------------------------------

def rollforward(work: WorkDir, month: str) -> None:
    check_month(month)
    if not work.warehouse_db.exists():
        raise RoiError(2, f"no warehouse at {work.warehouse_db}; run `roi build` first")
    with locked(work):
        promote_pending(work)
        before = check_marker(work)
        con = db_connect(work.warehouse_db, read_only=True)
        try:
            recorded = ingested(con, month)
            if recorded:
                if recorded != {p.name: manifest.sha256_file(p) for p in incoming_files(work, month).values()}:
                    raise RoiError(
                        2, f"month {month} already ingested with different content; restore from seed to change history"
                    )
                click.echo(f"{month} already ingested, identical")
                return
            check_expected(con, month)
        finally:
            con.close()

        staging = work.warehouse / "roi.staging.duckdb"
        remove_db(staging)  # left behind by an interrupted roll-forward
        shutil.copyfile(work.warehouse_db, staging)
        try:
            advance(work, staging, month)
            after = publish(work, staging, keep_previous=True)
        except BaseException:
            remove_db(staging)
            raise
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with (work.warehouse / "rollforward.log").open("a", encoding="utf-8") as f:
            f.write(f"{stamp} {month} before={before} after={after}\n")
        click.echo(f"rolled forward to {month}")
