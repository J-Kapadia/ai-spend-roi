"""`roi doctor`: each check on its own, then the whole `--tree` run on a toy build."""

import fcntl
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from conftest import connect
from roi import doctor
from roi.cli import main
from roi.workdir import RoiError, WorkDir

REPO = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures"


def roi(work, *args):
    return CliRunner().invoke(main, ["--work-dir", str(work), *args])


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    work = tmp_path_factory.mktemp("doctor")
    assert roi(work, "build", "--toy").exit_code == 0
    return work


@pytest.fixture
def copy(built, tmp_path):
    shutil.copytree(built, tmp_path / "w", symlinks=True)
    return tmp_path / "w"


# ---- lock ------------------------------------------------------------------------------------------------------

def test_the_lock_matches_this_repository():
    check = doctor.lock_matches(REPO)
    assert check.status == doctor.OK, check.detail


def write_project(root, deps, dev=("pytest",), locked=None, locked_dev=None):
    root.mkdir(exist_ok=True)
    deps_toml = ", ".join(f'"{d}"' for d in deps)
    dev_toml = ", ".join(f'"{d}"' for d in dev)
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "demo"\nversion = "0"\ndependencies = [{deps_toml}]\n[dependency-groups]\ndev = [{dev_toml}]\n')
    locked = deps if locked is None else locked
    locked_dev = dev if locked_dev is None else locked_dev
    reqs = "".join(f'    {{ name = "{d}" }},\n' for d in locked)
    devs = ", ".join(f'{{ name = "{d}" }}' for d in locked_dev)
    (root / "uv.lock").write_text(
        f'version = 1\n[[package]]\nname = "demo"\nversion = "0"\n[package.metadata]\nrequires-dist = [\n{reqs}]\n'
        f"[package.metadata.requires-dev]\ndev = [{devs}]\n")


def test_a_dependency_missing_from_the_lock_fails(tmp_path):
    write_project(tmp_path / "ok", ["click", "Jinja2>=3"], locked=["click", "jinja2"])
    assert doctor.lock_matches(tmp_path / "ok").status == doctor.OK
    write_project(tmp_path / "bad", ["click", "numpy"], locked=["click"])
    check = doctor.lock_matches(tmp_path / "bad")
    assert check.status == doctor.FAIL and "numpy" in check.detail and "uv lock" in check.detail
    write_project(tmp_path / "dev", ["click"], dev=["pytest", "ruff"], locked_dev=["pytest"])
    assert doctor.lock_matches(tmp_path / "dev").status == doctor.FAIL


def test_no_lock_files_is_a_skip(tmp_path):
    assert doctor.lock_matches(tmp_path).status == doctor.SKIP


# ---- books -----------------------------------------------------------------------------------------------------

def test_books_match_their_manifest(tmp_path):
    work = WorkDir(tmp_path)
    work.books.mkdir(parents=True)
    (work.books / "a.csv").write_text("x\n")
    import hashlib

    digest = hashlib.sha256(b"x\n").hexdigest()
    (work.data / "MANIFEST.sha256").write_text(f"{digest}  books/a.csv\n")
    check = doctor.books_match(work, tmp_path / "norepo")
    assert check.status == doctor.OK and "1 books files" in check.detail
    (work.books / "a.csv").write_text("y\n")
    assert doctor.books_match(work, tmp_path / "norepo").status == doctor.FAIL
    (work.books / "a.csv").unlink()
    assert "a.csv is missing" in doctor.books_match(work, tmp_path / "norepo").detail


def test_no_manifest_is_a_skip(tmp_path):
    assert doctor.books_match(WorkDir(tmp_path), tmp_path / "norepo").status == doctor.SKIP


# ---- warehouse -------------------------------------------------------------------------------------------------

def test_a_missing_warehouse_fails(tmp_path):
    check = doctor.warehouse_state(WorkDir(tmp_path))
    assert check.status == doctor.FAIL and "roi build" in check.detail


def test_a_held_lock_fails_and_a_leftover_file_warns(copy):
    work = WorkDir(copy)
    assert doctor.warehouse_state(work).status == doctor.OK
    (copy / "warehouse" / "roi.next.duckdb").write_bytes(b"")
    check = doctor.warehouse_state(work)
    assert check.status == doctor.WARN and "roi.next.duckdb" in check.detail and check.passed
    lock = copy / "warehouse" / ".lock"
    with lock.open("a") as holder:
        fcntl.flock(holder, fcntl.LOCK_EX)
        assert doctor.warehouse_state(work).status == doctor.FAIL
    assert doctor.warehouse_state(work).status == doctor.WARN


# ---- dbt, tie ------------------------------------------------------------------------------------------------

def test_dbt_passes_on_a_copy_and_leaves_the_published_file_alone(built):
    import hashlib

    db = built / "warehouse" / "roi.duckdb"
    before = hashlib.sha256(db.read_bytes()).hexdigest()
    check = doctor.dbt_tests(WorkDir(built))
    assert check.status == doctor.OK, check.detail
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before


def test_the_tie_holds_on_a_toy_build_and_fails_on_a_changed_ledger(copy):
    assert doctor.tie_holds(WorkDir(copy)).status == doctor.OK
    con = connect(str(copy / "warehouse" / "roi.duckdb"))
    con.execute("UPDATE raw_gl_journal SET debit_cents = debit_cents + 1 WHERE entry_id = (SELECT min(entry_id) FROM raw_gl_journal"
                " WHERE debit_cents > 0 AND account_code = (SELECT value FROM roi_settings WHERE key = 'gl_account'))")
    con.close()
    check = doctor.tie_holds(WorkDir(copy))
    assert check.status == doctor.FAIL and "team=" in check.detail


# ---- markers and registration ----------------------------------------------------------------------------------

def test_the_complete_marker(tmp_path):
    work = WorkDir(tmp_path)
    work.warehouse.mkdir()
    assert doctor.complete_marker(work).status == doctor.SKIP
    (work.warehouse / ".complete").write_text("")
    assert doctor.complete_marker(work).status == doctor.FAIL
    (work.warehouse / ".complete").write_text("abc\n")
    assert doctor.complete_marker(work).status == doctor.OK


def test_prereg_hash_must_equal_the_last_log_entry(tmp_path):
    (tmp_path / "config").mkdir()
    assert doctor.prereg(tmp_path).status == doctor.SKIP
    h1, h2 = "a" * 64, "b" * 64
    (tmp_path / "config" / "prereg.log").write_text(f"2026-10-13 seed 1 {h1}\n")
    (tmp_path / "config" / "prereg.sha256").write_text(f"{h1}\n")
    assert doctor.prereg(tmp_path).status == doctor.OK
    (tmp_path / "config" / "prereg.sha256").write_text(f"{h2}\n")
    assert doctor.prereg(tmp_path).status == doctor.FAIL
    (tmp_path / "config" / "prereg.log").write_text(f"2026-10-13 seed 1 {h1}\n2026-10-14 seed 2 {h2}\n")
    check = doctor.prereg(tmp_path)
    assert check.status == doctor.WARN and "2 registrations" in check.detail


# ---- the whole run -----------------------------------------------------------------------------------------------

def test_tree_runs_every_check_and_skips_on_request(copy):
    checks = doctor.tree(WorkDir(copy), REPO, skip=frozenset({"dbt", "truth_boundary"}))
    by_id = {c.id: c for c in checks}
    assert list(by_id) == ["lock", "books", "warehouse", "dbt", "tie", "truth_boundary", "complete", "prereg"]
    assert by_id["dbt"].detail == "skipped on request" and by_id["warehouse"].status == doctor.OK
    assert all(c.passed for c in checks)


def test_tree_without_books_or_a_warehouse_fails_both_checks_and_skips_those_that_need_the_warehouse(tmp_path):
    checks = {c.id: c for c in doctor.tree(WorkDir(tmp_path), REPO, skip=frozenset({"truth_boundary"}))}
    assert checks["books"].status == checks["warehouse"].status == doctor.FAIL
    assert checks["books"].detail.startswith("no books at")
    assert checks["dbt"].detail == checks["tie"].detail == "no warehouse"
    result = roi(tmp_path, "doctor", "--tree")
    assert result.exit_code == 1 and "check(s) failed: books, warehouse" in result.output


@pytest.mark.slow
def test_the_tree_command_passes_on_a_toy_build_in_text_and_json(built):
    result = roi(built, "doctor", "--tree")
    assert result.exit_code == 0, result.output
    assert "ok    dbt:" in result.output and "ok    truth_boundary:" in result.output
    assert result.output.rstrip().endswith("doctor --tree: ok")
    data = json.loads(roi(built, "doctor", "--json").output)
    assert data["command"] == "doctor" and data["status"] == "pass"
    assert [c["id"] for c in data["checks"]][:3] == ["lock", "books", "warehouse"]


# ---- --release ---------------------------------------------------------------------------------------------------

def test_release_without_git_exits_4(built, tmp_path):
    result = roi(tmp_path, "doctor", "--release")
    assert result.exit_code == 4
    assert "needs .git" in result.output
    with pytest.raises(RoiError) as e:
        doctor.release(WorkDir(tmp_path), REPO)
    assert e.value.exit_code == 4


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=True)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    git(root, "config", "user.email", "a@example.com")
    git(root, "config", "user.name", "A")
    git(root, "config", "commit.gpgsign", "false")
    (root / "README.md").write_text("x\n")
    git(root, "add", "README.md")
    git(root, "commit", "-q", "-m", "first")
    return root


def test_the_hook_check(repo):
    assert doctor.hook_installed(repo).status == doctor.FAIL
    (repo / "scripts" / "hooks").mkdir(parents=True)
    (repo / "scripts" / "hooks" / "pre-push").write_text("#!/bin/sh\n")
    git(repo, "config", "core.hooksPath", "scripts/hooks")
    assert doctor.hook_installed(repo).status == doctor.OK


def test_the_tree_must_be_clean(repo):
    assert doctor.clean_tree(repo).status == doctor.OK
    (repo / "stray.txt").write_text("x")
    check = doctor.clean_tree(repo)
    assert check.status == doctor.FAIL and "stray.txt" in check.detail


def test_the_prereg_hash_must_equal_the_one_at_the_latest_tag(repo):
    assert doctor.prereg_tag(repo).status == doctor.SKIP
    (repo / "config").mkdir()
    (repo / "config" / "prereg.sha256").write_text("a" * 64 + "\n")
    git(repo, "add", "config")
    git(repo, "commit", "-q", "-m", "prereg")
    assert doctor.prereg_tag(repo).status == doctor.FAIL  # no tag yet
    git(repo, "tag", "prereg-v1")
    assert doctor.prereg_tag(repo).status == doctor.OK
    (repo / "config" / "prereg.sha256").write_text("b" * 64 + "\n")
    check = doctor.prereg_tag(repo)
    assert check.status == doctor.FAIL and "prereg-v1" in check.detail


def test_the_signing_configuration_is_reported_and_needs_hardware_keys(repo):
    work = WorkDir(repo)
    git(repo, "config", "gpg.format", "ssh")
    assert doctor.signing(work).status == doctor.FAIL  # no allowed-signers yet
    (repo / "config").mkdir()
    shutil.copy(FIXTURES / "allowed-signers.software", repo / "config" / "allowed-signers")
    check = doctor.signing(work)
    assert check.status == doctor.FAIL and "gpg.format ssh" in check.detail and "ssh-ed25519" in check.detail
    shutil.copy(FIXTURES / "allowed-signers", repo / "config" / "allowed-signers")
    check = doctor.signing(work)
    assert check.status == doctor.OK and "user.signingkey unset" in check.detail and "1 keys" in check.detail


def test_release_adds_the_git_checks_to_the_tree_checks(built, tmp_path):
    root = tmp_path / "w"
    shutil.copytree(built, root, symlinks=True)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    checks = doctor.release(WorkDir(root), REPO, skip=frozenset({"dbt", "truth_boundary"}))
    ids = [c.id for c in checks]
    assert ids[:8] == ["lock", "books", "warehouse", "dbt", "tie", "truth_boundary", "complete", "prereg"]
    assert ids[8:] == ["hook", "prereg_tag", "signing", "checkpoint", "clean_tree", "signoff"]
    failed = {c.id for c in checks if not c.passed}
    assert {"hook", "signing", "clean_tree", "signoff"} <= failed
