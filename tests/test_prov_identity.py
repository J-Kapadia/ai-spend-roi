"""Provenance identity: every memo number has an immutable record and at least one build row; a rebuild
at another commit or a roll-forward leaves the records of closed months as they were."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from conftest import connect
from roi import numbers, rollforward
from roi.cli import main
from roi.steps import roi as roi_step
from roi.workdir import WorkDir

FIXTURE = Path(__file__).parent / "fixtures" / "gen_fixture.yaml"
ROI_FIXTURE = Path(__file__).parent / "fixtures" / "roi_fixture.yaml"
CLOSED = "2026-03-01"
GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
}


def roi(work, *args):
    return CliRunner().invoke(main, ["--work-dir", str(work), *args])


def db(work) -> Path:
    return work / "warehouse" / "roi.duckdb"


def fetch(work, sql, *params):
    con = connect(str(db(work)), read_only=True)
    try:
        return con.execute(sql, list(params)).fetchall()
    finally:
        con.close()


def git(repo, *args):
    env = {**os.environ, **GIT_ENV}
    subprocess.run(["git", *args], cwd=repo, env=env, check=True, capture_output=True)


def repo_at(path, monkeypatch, *, files=("a.txt",)) -> Path:
    path.mkdir()
    git(path, "init", "-q")
    for name in files:
        (path / name).write_text(name)
    git(path, "add", ".")
    git(path, "commit", "-q", "-m", "first")
    monkeypatch.chdir(path)
    return path


def toy_build(work) -> None:
    result = roi(work, "build", "--toy")
    assert result.exit_code == 0, result.output


def shas(work) -> set[str]:
    return {sha for (sha,) in fetch(work, "SELECT DISTINCT git_sha FROM prov_build")}


def test_every_enumerated_number_has_a_record_and_a_build(tmp_path):
    work = tmp_path / "w"
    toy_build(work)
    con = connect(str(db(work)), read_only=True)
    try:
        through = con.execute("SELECT max(through_month) FROM est_team_effect").fetchone()[0]
        config = roi_step.config_path(WorkDir(work))
        listed = numbers.enumerate_numbers(con, through, config if config.exists() else None)
        assert len(listed) > 1
        for n in listed:
            rows = con.execute(
                "SELECT count(*) FROM prov_record r JOIN prov_build b USING (prov_id) WHERE r.name = ?"
                " AND strftime(r.through_month, '%Y-%m') = ? AND r.display_value = ?",
                [n.name, through, n.display_value],
            ).fetchone()[0]
            assert rows >= 1, n.name
        records = con.execute("SELECT count(*) FROM prov_record").fetchone()[0]
        orphans = con.execute("SELECT count(*) FROM prov_build WHERE prov_id NOT IN (SELECT prov_id FROM prov_record)")
        assert records == len(listed) and orphans.fetchone()[0] == 0
    finally:
        con.close()


def test_a_rebuild_at_another_commit_keeps_the_content_hash_and_changes_the_git_sha(tmp_path, monkeypatch):
    first = repo_at(tmp_path / "one", monkeypatch)
    toy_build(tmp_path / "w1")
    (first / "b.txt").write_text("more")
    git(first, "add", ".")
    git(first, "commit", "-q", "-m", "second")
    toy_build(tmp_path / "w2")
    assert rollforward.content_hash(db(tmp_path / "w1")) == rollforward.content_hash(db(tmp_path / "w2"))
    one, two = shas(tmp_path / "w1"), shas(tmp_path / "w2")
    assert len(one) == len(two) == 1 and one != two
    assert not any(s.endswith("-dirty") for s in one | two)


def test_a_build_in_a_dirty_tree_marks_the_git_sha(tmp_path, monkeypatch):
    repo = repo_at(tmp_path / "repo", monkeypatch)
    (repo / "a.txt").write_text("edited")
    toy_build(tmp_path / "w")
    (sha,) = shas(tmp_path / "w")
    assert sha.endswith("-dirty") and len(sha) > len("-dirty")


def test_a_build_outside_git_writes_no_git(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))
    toy_build(tmp_path / "w")
    assert shas(tmp_path / "w") == {"no-git"}


@pytest.fixture(scope="module")
def base(tmp_path_factory):
    work = tmp_path_factory.mktemp("base")
    (work / "config").mkdir()
    shutil.copy(ROI_FIXTURE, work / "config" / "roi.yaml")
    result = roi(work, "build", "--config", str(FIXTURE))
    assert result.exit_code == 0, result.output
    return work


def test_a_rollforward_leaves_the_closed_records_and_appends_the_new_month(base, tmp_path):
    work = tmp_path / "w"
    shutil.copytree(base, work, symlinks=True)
    sql = "SELECT * FROM prov_record WHERE through_month <= DATE '2026-03-01' ORDER BY ALL"
    before = fetch(work, sql)
    builds = fetch(work, "SELECT * FROM prov_build ORDER BY ALL")
    assert before and {b[4] for b in builds} == {"roi build"}
    result = roi(work, "rollforward", "--month", "2026-04")
    assert result.exit_code == 0, result.output
    assert fetch(work, sql) == before
    assert fetch(work, "SELECT * FROM prov_build WHERE prov_id IN (SELECT prov_id FROM prov_record"
                       " WHERE through_month <= DATE '2026-03-01') ORDER BY ALL") == builds
    new = fetch(work, "SELECT prov_id FROM prov_record WHERE through_month = DATE '2026-04-01'")
    assert new
    producers = fetch(
        work, "SELECT DISTINCT producer_id FROM prov_build WHERE prov_id IN"
              " (SELECT prov_id FROM prov_record WHERE through_month = DATE '2026-04-01')")
    assert producers == [("roi rollforward",)]


def stored_id(work) -> str:
    return fetch(work, "SELECT prov_id FROM prov_record WHERE name LIKE 'effect:%' ORDER BY prov_id")[0][0]


def test_explain_prints_the_build_and_a_changed_source_row_fails_it(base, tmp_path):
    work = tmp_path / "w"
    shutil.copytree(base, work, symlinks=True)
    pid = stored_id(work)
    ok = roi(work, "explain", pid[:8])
    assert ok.exit_code == 0, ok.output
    assert "  build " in ok.output and "roi build" in ok.output and "derived" not in ok.output
    data = json.loads(roi(work, "explain", pid, "--json").output)
    assert data["record"]["builds"] and data["record"]["stored"] is True

    con = connect(str(db(work)))
    con.execute("UPDATE est_team_effect SET effect = effect + 0.5")
    con.close()
    bad = roi(work, "explain", pid)
    assert bad.exit_code == 1 and "checksum mismatch" in bad.output
