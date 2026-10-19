"""Sign-off: the file is a record, the control is the signed commit that adds it (verified here
against allowed-signers files made at test time, plus the hardware and software fixture files)."""

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from roi import memo, signoff
from roi.cli import main
from roi.workdir import RoiError, WorkDir

FIXTURES = Path(__file__).parent / "fixtures"
HARDWARE = FIXTURES / "allowed-signers"
SOFTWARE = FIXTURES / "allowed-signers.software"
MONTH = "2026-03"

needs_ssh = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="ssh-keygen is not installed")

ROI_YAML = """
memo_budget_usd: 1000
value_per_unit:
  alpha: {low: 5.0, base: 10.0, high: 15.0}
  beta: {low: 5.0, base: 10.0, high: 15.0}
  gamma: {low: 5.0, base: 10.0, high: 15.0}
"""


def git(root, *args, check=True):
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=check)


@pytest.fixture
def repo(tmp_path):
    """A git repository with a built-looking site and a software signing key."""
    root = tmp_path / "repo"
    (root / "site" / "prov").mkdir(parents=True)
    (root / "site" / "index.html").write_text("<html><body>memo</body></html>")
    (root / "site" / "prov" / "ab.html").write_text("<html><body>page</body></html>")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    git(root, "config", "user.email", "reviewer@example.com")
    git(root, "config", "user.name", "Reviewer")
    git(root, "config", "commit.gpgsign", "false")
    return WorkDir(root)


@pytest.fixture
def keys(tmp_path):
    key = tmp_path / "key"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "test", "-f", str(key)], check=True)
    signers = tmp_path / "signers"
    signers.write_text(f"reviewer@example.com namespaces=\"git\" {' '.join(key.with_suffix('.pub').read_text().split()[:2])}\n")
    return key, signers


def commit_signoff(work, key=None):
    git(work.root, "add", f"signoffs/{MONTH}.yaml")
    if key is None:
        git(work.root, "commit", "-q", "-m", "signoff")
    else:
        git(work.root, "-c", "gpg.format=ssh", "-c", f"user.signingkey={key}.pub", "commit", "-q", "-S", "-m", "signoff")


def write(work):
    return signoff.write(work, MONTH, "A. Reviewer", "2026-10-13", ["bb", "aa", "bb"])


# ---- the record ------------------------------------------------------------------------------------

def test_the_signoff_file_holds_name_date_memo_hash_and_the_prov_ids(repo):
    f = write(repo)
    assert f == repo.root / "signoffs" / f"{MONTH}.yaml"
    rec = yaml.safe_load(f.read_text())
    assert rec == {"month": MONTH, "name": "A. Reviewer", "date": "2026-10-13",
                   "memo_sha256": signoff.memo_hash(repo.site), "prov_ids": ["aa", "bb"]}


def test_the_memo_hash_follows_every_file_of_the_site(repo):
    before = signoff.memo_hash(repo.site)
    (repo.site / "prov" / "ab.html").write_text("<html><body>changed</body></html>")
    assert signoff.memo_hash(repo.site) != before


def test_a_signoff_is_never_rewritten(repo):
    write(repo)
    with pytest.raises(RoiError, match="already exists") as e:
        write(repo)
    assert e.value.exit_code == 2


def test_writing_a_signoff_needs_a_built_site(tmp_path):
    with pytest.raises(RoiError, match="no memo"):
        signoff.write(WorkDir(tmp_path), MONTH, "A", "2026-10-13", [])


def test_the_state_is_pending_recorded_or_stale(repo):
    assert signoff.state(repo, MONTH).startswith("pending")
    write(repo)
    assert signoff.state(repo, MONTH) == "recorded by A. Reviewer on 2026-10-13"
    (repo.site / "index.html").write_text("<html><body>edited</body></html>")
    assert signoff.state(repo, MONTH).startswith("stale")


# ---- the allowed-signers fixtures ------------------------------------------------------------------------

def test_the_fixture_lists_one_hardware_key_and_the_software_fixture_one_software_key():
    assert signoff.key_types(HARDWARE) == ["sk-ssh-ed25519@openssh.com"]
    assert signoff.key_types(SOFTWARE) == ["ssh-ed25519"]
    assert signoff.hardware_problems(HARDWARE) == []
    assert "not hardware-backed (ssh-ed25519)" in signoff.hardware_problems(SOFTWARE)[0]


def test_an_empty_or_missing_signers_file_is_a_problem(tmp_path):
    empty = tmp_path / "signers"
    empty.write_text("# nothing\n")
    assert "lists no keys" in signoff.hardware_problems(empty)[0]
    assert "does not exist" in signoff.hardware_problems(tmp_path / "absent")[0]


# ---- verification ----------------------------------------------------------------------------------------------

def test_no_signoff_file_is_reported(repo):
    assert signoff.verify(repo, MONTH, HARDWARE) == [f"no sign-off for {MONTH}: signoffs/{MONTH}.yaml is missing"]


def test_a_software_key_is_rejected_when_hardware_is_required(repo):
    write(repo)
    problems = signoff.verify(repo, MONTH, SOFTWARE)
    assert any("not hardware-backed" in p for p in problems)


@needs_ssh
def test_a_commit_signed_by_a_listed_key_verifies(repo, keys):
    key, signers = keys
    write(repo)
    commit_signoff(repo, key)
    assert signoff.verify(repo, MONTH, signers, require_hardware=False) == []
    # the same signed commit is refused when only hardware keys may attest
    assert any("not hardware-backed" in p for p in signoff.verify(repo, MONTH, signers))


@needs_ssh
def test_a_commit_signed_by_an_unlisted_key_is_refused(repo, keys):
    key, _ = keys
    write(repo)
    commit_signoff(repo, key)
    problems = signoff.verify(repo, MONTH, HARDWARE)  # the fixture's hardware key did not sign it
    assert len(problems) == 1 and "is not signed by a key in allowed-signers" in problems[0]


@needs_ssh
def test_an_unsigned_commit_is_refused(repo, keys):
    _, signers = keys
    write(repo)
    commit_signoff(repo)
    problems = signoff.verify(repo, MONTH, signers, require_hardware=False)
    assert len(problems) == 1 and "is not signed" in problems[0]


def test_a_hand_written_uncommitted_file_is_refused(repo, tmp_path):
    signers = tmp_path / "signers"
    signers.write_text(SOFTWARE.read_text())
    write(repo)
    git(repo.root, "commit", "-q", "--allow-empty", "-m", "first")
    problems = signoff.verify(repo, MONTH, signers, require_hardware=False)
    assert problems == [f"signoffs/{MONTH}.yaml is not committed"]


@needs_ssh
def test_a_file_edited_after_it_was_committed_is_refused(repo, keys):
    key, signers = keys
    f = write(repo)
    commit_signoff(repo, key)
    f.write_text(f.read_text().replace("A. Reviewer", "Someone Else"))
    assert "differs from the file in the commit" in signoff.verify(repo, MONTH, signers, require_hardware=False)[0]


@needs_ssh
def test_a_memo_changed_after_the_signoff_is_refused(repo, keys):
    key, signers = keys
    write(repo)
    commit_signoff(repo, key)
    (repo.site / "index.html").write_text("<html><body>edited</body></html>")
    assert "the memo has changed since the sign-off" in signoff.verify(repo, MONTH, signers, require_hardware=False)[0]


def test_verification_without_git_exits_4(tmp_path):
    work = WorkDir(tmp_path)
    (tmp_path / "site").mkdir()
    (tmp_path / "site" / "index.html").write_text("<html></html>")
    write(work)
    with pytest.raises(RoiError, match="without .git") as e:
        signoff.verify(work, MONTH, SOFTWARE, require_hardware=False)
    assert e.value.exit_code == 4


# ---- the commands ------------------------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def built(tmp_path_factory):
    work = tmp_path_factory.mktemp("signoff")
    (work / "config").mkdir()
    (work / "config" / "roi.yaml").write_text(ROI_YAML)
    # built outside any repository, so the record of the build never says the tree was dirty
    with pytest.MonkeyPatch.context() as mp:
        mp.chdir(tmp_path_factory.mktemp("cwd"))
        out = CliRunner().invoke(main, ["--work-dir", str(work), "build", "--toy"])
    assert out.exit_code == 0, out.output
    assert CliRunner().invoke(main, ["--work-dir", str(work), "memo", "--month", "2025-06"]).exit_code == 0
    return work


def build_in_repo(work, repo, dirty):
    """Build `work` with `repo` as the current directory, which is where the build reads its git sha."""
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    git(repo, "-c", "user.email=a@example.com", "-c", "user.name=A", "-c", "commit.gpgsign=false",
        "commit", "-q", "--allow-empty", "-m", "first")
    if dirty:
        (repo / "scratch.txt").write_text("uncommitted\n")
    with pytest.MonkeyPatch.context() as mp:
        mp.chdir(repo)
        out = CliRunner().invoke(main, ["--work-dir", str(work), "build", "--toy"])
    assert out.exit_code == 0, out.output
    assert CliRunner().invoke(main, ["--work-dir", str(work), "memo", "--month", "2025-06"]).exit_code == 0


def test_signoff_refuses_a_non_interactive_run(built, tmp_path):
    shutil.copytree(built, tmp_path / "w", symlinks=True)
    result = CliRunner().invoke(main, ["--work-dir", str(tmp_path / "w"), "signoff", "--month", "2025-06"], input="Me\ny\n")
    assert result.exit_code == 2
    assert "needs a person at a terminal" in result.output
    assert not (tmp_path / "w" / "signoffs").exists()


def test_signoff_rejects_a_bad_month(built):
    assert CliRunner().invoke(main, ["--work-dir", str(built), "signoff", "--month", "June"]).exit_code == 2


def test_a_person_at_a_terminal_writes_the_file_citing_every_number(built, tmp_path, monkeypatch):
    shutil.copytree(built, tmp_path / "w", symlinks=True)
    monkeypatch.setattr("roi.commands.signoff._interactive", lambda: True)
    result = CliRunner().invoke(main, ["--work-dir", str(tmp_path / "w"), "signoff", "--month", "2025-06"], input="A. Person\ny\n")
    assert result.exit_code == 0, result.output
    rec = yaml.safe_load((tmp_path / "w" / "signoffs" / "2025-06.yaml").read_text())
    assert rec["name"] == "A. Person" and rec["month"] == "2025-06"
    ids = {t["id"] for t in _tags(tmp_path / "w")}
    assert set(rec["prov_ids"]) == ids
    assert "git commit -S" in result.output
    again = CliRunner().invoke(main, ["--work-dir", str(tmp_path / "w"), "signoff", "--month", "2025-06"], input="A. Person\ny\n")
    assert again.exit_code == 2 and "already exists" in again.output


def test_declining_the_confirmation_writes_nothing(built, tmp_path, monkeypatch):
    shutil.copytree(built, tmp_path / "w", symlinks=True)
    monkeypatch.setattr("roi.commands.signoff._interactive", lambda: True)
    result = CliRunner().invoke(main, ["--work-dir", str(tmp_path / "w"), "signoff", "--month", "2025-06"], input="A. Person\nn\n")
    assert result.exit_code == 2 and not (tmp_path / "w" / "signoffs").exists()


def _tags(work):
    s = memo._MemoScan()
    s.feed((work / "site" / "index.html").read_text(encoding="utf-8"))
    return s.tagged


@needs_ssh
def test_memo_require_signoff_accepts_a_signed_commit_and_the_check_reports_it(built, tmp_path, keys, monkeypatch):
    key, signers = keys
    work = tmp_path / "w"
    shutil.copytree(built, work, symlinks=True)
    monkeypatch.setattr("roi.commands.signoff._interactive", lambda: True)
    assert CliRunner().invoke(main, ["--work-dir", str(work), "signoff", "--month", "2025-06"], input="A. Person\ny\n").exit_code == 0
    subprocess.run(["git", "init", "-q", str(work)], check=True)
    git(work, "config", "user.email", "reviewer@example.com")
    git(work, "config", "user.name", "Reviewer")
    git(work, "add", "signoffs/2025-06.yaml")
    git(work, "-c", "gpg.format=ssh", "-c", f"user.signingkey={key}.pub", "commit", "-q", "-S", "-m", "signoff: 2025-06")
    (work / "config").mkdir(exist_ok=True)
    # the repository's rule is hardware keys only: the software test key must be refused
    shutil.copy(signers, work / "config" / "allowed-signers")
    refused = CliRunner().invoke(main, ["--work-dir", str(work), "memo", "--month", "2025-06", "--require-signoff"])
    assert refused.exit_code == 1 and "not hardware-backed" in refused.output
    # a pending/recorded sign-off is allowed by a plain check
    plain = CliRunner().invoke(main, ["--work-dir", str(work), "memo", "--month", "2025-06", "--check"])
    assert plain.exit_code == 0 and "sign-off: recorded by A. Person" in plain.output
    # and the signature itself verifies once software keys are allowed
    assert signoff.verify(WorkDir(work), "2025-06", signers, require_hardware=False) == []


def _signed_work(tmp_path, keys, monkeypatch, *, dirty):
    key, signers = keys
    work = tmp_path / "w"
    (work / "config").mkdir(parents=True)
    (work / "config" / "roi.yaml").write_text(ROI_YAML)
    build_in_repo(work, tmp_path / "builder", dirty)
    monkeypatch.setattr("roi.commands.signoff._interactive", lambda: True)
    assert CliRunner().invoke(main, ["--work-dir", str(work), "signoff", "--month", "2025-06"], input="A. Person\ny\n").exit_code == 0
    subprocess.run(["git", "init", "-q", str(work)], check=True)
    git(work, "config", "user.email", "reviewer@example.com")
    git(work, "config", "user.name", "Reviewer")
    git(work, "add", "signoffs/2025-06.yaml")
    git(work, "-c", "gpg.format=ssh", "-c", f"user.signingkey={key}.pub", "commit", "-q", "-S", "-m", "signoff: 2025-06")
    return work, signers


@needs_ssh
def test_a_build_on_a_dirty_tree_cannot_be_signed_off(tmp_path, keys, monkeypatch):
    work, signers = _signed_work(tmp_path, keys, monkeypatch, dirty=True)
    # the sign-off itself is valid: the signed commit verifies against a listed key
    assert [p for p in signoff.verify(WorkDir(work), "2025-06", signers, require_hardware=False) if "dirty" not in p] == []
    shutil.copy(HARDWARE, work / "config" / "allowed-signers")
    result = CliRunner().invoke(main, ["--work-dir", str(work), "memo", "--month", "2025-06", "--require-signoff"])
    assert result.exit_code == 1
    assert "the build of 2025-06 was made on a dirty tree (git " in result.output and "-dirty)" in result.output


@needs_ssh
def test_a_build_on_a_clean_tree_is_not_held_back(tmp_path, keys, monkeypatch):
    work, signers = _signed_work(tmp_path, keys, monkeypatch, dirty=False)
    assert signoff.verify(WorkDir(work), "2025-06", signers, require_hardware=False) == []
