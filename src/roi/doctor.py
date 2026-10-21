"""Health checks for `roi doctor`: `--tree` needs only an extraction, `--release`
adds the checks that need `.git`. Each check is ok, warn, skip (its input does not exist here,
and it says what) or fail; only a fail makes the command exit non-zero.

Nothing here reads the truth file. The truth boundary is checked by running its test.
"""

from __future__ import annotations

import fcntl
import hashlib
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import duckdb

from roi import signoff, verify_books
from roi.steps import BuildContext
from roi.steps import dbt as dbt_step
from roi.workdir import RoiError, WorkDir

REPO = Path(__file__).resolve().parents[2]
OK, WARN, SKIP, FAIL = "ok", "warn", "skip", "fail"


@dataclass(frozen=True)
class Check:
    id: str
    status: str
    detail: str

    @property
    def passed(self) -> bool:
        return self.status != FAIL


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _requirement_name(spec: str) -> str:
    return _norm(re.split(r"[<>=!~\[;@ ]", spec.strip(), maxsplit=1)[0])


# ---- --tree --------------------------------------------------------------------------------------

def lock_matches(repo: Path = REPO) -> Check:
    """The uv lock lists the same direct dependencies as pyproject.toml (read from the files; no network)."""
    pyproject, lock = repo / "pyproject.toml", repo / "uv.lock"
    if not pyproject.exists() or not lock.exists():
        return Check("lock", SKIP, "no pyproject.toml and uv.lock beside the source")
    project = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    name = _norm(project["project"]["name"])
    root = next((p for p in tomllib.loads(lock.read_text(encoding="utf-8")).get("package", [])
                 if _norm(p["name"]) == name), None)
    if root is None:
        return Check("lock", FAIL, f"uv.lock has no package {name}")
    meta = root.get("metadata", {})
    want = {_requirement_name(d) for d in project["project"].get("dependencies", [])}
    have = {_norm(d["name"]) for d in meta.get("requires-dist", [])}
    want_dev = {_requirement_name(d) for d in project.get("dependency-groups", {}).get("dev", []) if isinstance(d, str)}
    have_dev = {_norm(d["name"]) for d in meta.get("requires-dev", {}).get("dev", [])}
    problems = [f"{sorted(a ^ b)}" for a, b in ((want, have), (want_dev, have_dev)) if a != b]
    if problems:
        return Check("lock", FAIL, f"uv.lock disagrees with pyproject.toml on: {'; '.join(problems)}; run `uv lock`")
    return Check("lock", OK, f"uv.lock lists the {len(want)} dependencies and {len(want_dev)} dev dependencies of pyproject.toml")


def books_match(work: WorkDir, repo: Path = REPO) -> Check:
    """The books equal the committed manifest: lines of `<sha256>  <path>`, paths relative to the manifest."""
    manifest = next((m for m in (work.data / "MANIFEST.sha256", repo / "data" / "MANIFEST.sha256") if m.exists()), None)
    if manifest is None:
        return Check("books", SKIP, "no data/MANIFEST.sha256 beside the books")
    if not work.books.exists():
        return Check("books", FAIL, f"no books at {work.books}; run `roi gen`")
    bad, n = [], 0
    for line in manifest.read_text(encoding="utf-8").splitlines():
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        digest, rel = parts[0], parts[1].strip().lstrip("*")
        n += 1
        target = next((c for c in (work.data / rel, work.books / rel, work.books / Path(rel).name) if c.is_file()), None)
        if target is None:
            bad.append(f"{rel} is missing")
        elif hashlib.sha256(target.read_bytes()).hexdigest() != digest:
            bad.append(f"{rel} differs")
    if bad:
        return Check("books", FAIL, f"the books do not match {manifest.name}: {', '.join(bad)}")
    return Check("books", OK, f"{n} books files match {manifest.name}")


def warehouse_state(work: WorkDir) -> Check:
    """The published warehouse exists, no process holds its lock, and no half-built file is left."""
    if not work.warehouse_db.exists():
        return Check("warehouse", FAIL, f"no warehouse at {work.warehouse_db}; run `roi build`")
    lock = work.warehouse / ".lock"
    if lock.exists():
        with lock.open("a") as f:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return Check("warehouse", FAIL, f"{lock} is held by a running process")
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
    left = [p.name for p in (work.warehouse_next_db, work.warehouse / "roi.staging.duckdb") if p.exists()]
    if left:
        return Check("warehouse", WARN, f"{work.warehouse_db.name} exists; {', '.join(left)} is left from an interrupted run")
    return Check("warehouse", OK, f"{work.warehouse_db} exists and no lock is held")


def dbt_tests(work: WorkDir) -> Check:
    """Every dbt model and test passes, run on a copy so the published file is not touched."""
    with tempfile.TemporaryDirectory() as tmp:
        scratch = WorkDir(Path(tmp))
        scratch.warehouse.mkdir()
        shutil.copy2(work.warehouse_db, scratch.warehouse_next_db)
        settings = work.warehouse / "build_settings.json"
        if settings.exists():
            shutil.copy2(settings, scratch.warehouse / settings.name)
        try:
            dbt_step.run(BuildContext(work=scratch, db=scratch.warehouse_next_db, through=""))
        except RoiError as e:
            return Check("dbt", FAIL, e.message)
    return Check("dbt", OK, "dbt build passes its models and tests on a copy of the warehouse")


def tie_holds(work: WorkDir) -> Check:
    con = verify_books.open_warehouse(work)
    try:
        months = verify_books.result_months(con)
        if not months:
            return Check("tie", SKIP, "the warehouse has no result months")
        report = verify_books.check_gl_tie(con, months[-1])
    except RoiError as e:
        return Check("tie", FAIL, e.message)
    finally:
        con.close()
    return Check("tie", OK, f"{report.team_months} team-months tie to the ledger to the cent through {months[-1]}")


def truth_boundary(repo: Path = REPO) -> Check:
    test = repo / "tests" / "test_truth_boundary.py"
    if not test.exists():
        return Check("truth_boundary", SKIP, "no tests/test_truth_boundary.py beside the source")
    run = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(test)],
                         cwd=repo, capture_output=True, text=True, check=False)
    if run.returncode != 0:
        return Check("truth_boundary", FAIL, "tests/test_truth_boundary.py fails:\n" + run.stdout[-1500:])
    return Check("truth_boundary", OK, "tests/test_truth_boundary.py passes")


def complete_marker(work: WorkDir) -> Check:
    """`warehouse/.complete` names the published file. Its content is written by the file swap, so
    this check can only ask that it exists once a swap has written one."""
    marker = work.warehouse / ".complete"
    if not marker.exists():
        return Check("complete", SKIP, "no warehouse/.complete; the build has not written one")
    if not marker.read_text(encoding="utf-8").strip():
        return Check("complete", FAIL, "warehouse/.complete is empty")
    return Check("complete", OK, "warehouse/.complete is present")


def prereg(repo: Path = REPO) -> Check:
    """`config/prereg.sha256` equals the hash on the last line of `config/prereg.log`; two or more registrations warn."""
    current, log = repo / "config" / "prereg.sha256", repo / "config" / "prereg.log"
    if not current.exists() or not log.exists():
        return Check("prereg", SKIP, "no config/prereg.sha256 and prereg.log")
    want = current.read_text(encoding="utf-8").split()[0] if current.read_text(encoding="utf-8").split() else ""
    entries = [line for line in log.read_text(encoding="utf-8").splitlines() if re.search(r"\b[0-9a-f]{64}\b", line)]
    if not entries or want not in entries[-1]:
        return Check("prereg", FAIL, "config/prereg.sha256 is not the hash of the last entry of config/prereg.log")
    if len(entries) >= 2:
        return Check("prereg", WARN, f"{len(entries)} registrations in config/prereg.log; each further one weakens the pre-registration")
    return Check("prereg", OK, "config/prereg.sha256 matches the last entry of config/prereg.log")


def tree(work: WorkDir, repo: Path = REPO, skip: frozenset[str] = frozenset()) -> list[Check]:
    steps: list[tuple[str, Callable[[], Check]]] = [
        ("lock", lambda: lock_matches(repo)),
        ("books", lambda: books_match(work, repo)),
        ("warehouse", lambda: warehouse_state(work)),
    ]
    checks = []
    for name, fn in steps:
        checks.append(fn() if name not in skip else Check(name, SKIP, "skipped on request"))
    if checks[-1].status == FAIL:  # no warehouse: nothing below can run
        checks += [Check(i, SKIP, "no warehouse") for i in ("dbt", "tie", "truth_boundary", "complete")]
    else:
        later: list[tuple[str, Callable[[], Check]]] = [
            ("dbt", lambda: dbt_tests(work)),
            ("tie", lambda: tie_holds(work)),
            ("truth_boundary", lambda: truth_boundary(repo)),
            ("complete", lambda: complete_marker(work)),
        ]
        checks += [fn() if name not in skip else Check(name, SKIP, "skipped on request") for name, fn in later]
    checks.append(prereg(repo) if "prereg" not in skip else Check("prereg", SKIP, "skipped on request"))
    return checks


# ---- --release -----------------------------------------------------------------------------------

def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=False)


def hook_installed(root: Path) -> Check:
    hooks = _git(root, "config", "core.hooksPath").stdout.strip()
    pre_push = (root / hooks / "pre-push") if hooks else None
    if not hooks or pre_push is None or not pre_push.is_file():
        return Check("hook", FAIL, "the pre-push hook is not installed; run scripts/install-hooks.sh")
    return Check("hook", OK, f"core.hooksPath = {hooks} and it holds a pre-push hook")


def prereg_tag(root: Path) -> Check:
    current = root / "config" / "prereg.sha256"
    if not current.exists():
        return Check("prereg_tag", SKIP, "no config/prereg.sha256")
    tags = sorted(
        (t for t in _git(root, "tag", "--list", "prereg-v*").stdout.split() if re.fullmatch(r"prereg-v\d+", t)),
        key=lambda t: int(t.removeprefix("prereg-v")),
    )
    if not tags:
        return Check("prereg_tag", FAIL, "config/prereg.sha256 exists but there is no prereg-v<n> tag")
    pinned = _git(root, "show", f"{tags[-1]}:config/prereg.sha256").stdout.strip()
    if pinned != current.read_text(encoding="utf-8").strip():
        return Check("prereg_tag", FAIL, f"config/prereg.sha256 differs from the one at {tags[-1]}")
    return Check("prereg_tag", OK, f"config/prereg.sha256 equals the one at {tags[-1]}")


def signing(work: WorkDir) -> Check:
    fmt = _git(work.root, "config", "gpg.format").stdout.strip() or "(unset)"
    key = "set" if _git(work.root, "config", "user.signingkey").stdout.strip() else "unset"
    signers = signoff.signers_path(work)
    problems = signoff.hardware_problems(signers)
    summary = f"gpg.format {fmt}, user.signingkey {key}, {signers.name}: {len(signoff.key_types(signers)) if signers.exists() else 0} keys"
    if problems:
        return Check("signing", FAIL, f"{summary}; {'; '.join(problems)}")
    return Check("signing", OK, summary)


def clean_tree(root: Path) -> Check:
    if not (root / ".git").exists():
        return Check("clean_tree", SKIP, "no .git at the project root")
    dirty = _git(root, "status", "--porcelain").stdout.splitlines()
    if dirty:
        return Check("clean_tree", FAIL, f"{len(dirty)} uncommitted changes, first: {dirty[0]}")
    return Check("clean_tree", OK, "the working tree is clean")


def signoff_present(work: WorkDir) -> Check:
    con = verify_books.open_warehouse(work)
    try:
        months = verify_books.result_months(con)
    finally:
        con.close()
    if not months:
        return Check("signoff", SKIP, "the warehouse has no result months")
    problems = signoff.verify(work, months[-1])
    if problems:
        return Check("signoff", FAIL, "; ".join(problems))
    return Check("signoff", OK, f"the sign-off of {months[-1]} is recorded, current and signed")


def release(work: WorkDir, repo: Path = REPO, skip: frozenset[str] = frozenset()) -> list[Check]:
    """The tree checks plus those that need .git. Raises RoiError(4) without .git."""
    if not signoff.in_git(work.root):
        raise RoiError(4, f"roi doctor --release needs .git in {work.root}")
    checks = tree(work, repo, skip)
    checks += [
        hook_installed(work.root),
        prereg_tag(work.root),
        signing(work),
        Check("checkpoint", SKIP, "this repository keeps no checkpoint"),
        clean_tree(work.root),
    ]
    checks.append(signoff_present(work) if work.warehouse_db.exists() else Check("signoff", SKIP, "no warehouse"))
    return checks
