"""Two `roi build` processes on separate work dirs run side by side without touching each other
or the repository, and finish with the same warehouse content and the same memo bytes.

The advisory lock is per work dir (`warehouse/.lock`), so only a second process on the *same* work
dir exits 2 "warehouse busy" (tested in test_rollforward.py). dbt runs as a subprocess with its own
`target/` and `logs/` under each work dir's `warehouse/dbt/`.
"""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from roi import rollforward
from roi.workdir import WorkDir

pytestmark = pytest.mark.slow

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "gen_fixture.yaml"
ROI_FIXTURE = ROOT / "tests" / "fixtures" / "roi_fixture.yaml"
RUN = "import sys; from roi.cli import main; main(sys.argv[1:])"
TIMEOUT = 600
# What a build or a memo must leave out of the repository root: a stray one means a path
# resolved against the current directory instead of the work dir.
STRAYS = ("target", "logs", "warehouse", "site")


def roi_args(*args):
    return [sys.executable, "-c", RUN, *args]


def start(work, *build_args):
    return subprocess.Popen(
        roi_args("build", *build_args, "--work-dir", str(work)),
        cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )


def finish(procs):
    """Wait for every process, killing the rest when one hangs, and return each (code, out, err)."""
    results = []
    try:
        for p in procs:
            out, err = p.communicate(timeout=TIMEOUT)
            results.append((p.returncode, out, err))
    finally:
        for p in procs:
            if p.poll() is None:
                p.kill()
                p.communicate()
    return results


def repo_strays():
    """Every file under the stray names at the repository root, with its mtime: a checkout that
    already has a `site/` or `warehouse/` from a local run is unchanged when nothing wrote to it."""
    found = {}
    for name in STRAYS:
        top = ROOT / name
        if top.exists():
            found[name] = top.stat().st_mtime_ns
            found.update({str(p): p.stat().st_mtime_ns for p in top.rglob("*")})
    return found


@pytest.mark.parametrize(
    "build_args, month",
    [(("--toy",), "2025-06"), (("--config", str(FIXTURE)), "2026-03")],  # the last month each company has results for
    ids=["toy", "fixture-company"],
)
def test_two_builds_on_separate_work_dirs_do_not_collide(tmp_path, build_args, month):
    a, b = tmp_path / "A", tmp_path / "B"
    for work in (a, b):
        if "--config" in build_args:  # the fixture company's values per unit are read from the work dir
            (work / "config").mkdir(parents=True)
            shutil.copy(ROI_FIXTURE, work / "config" / "roi.yaml")
    before = repo_strays()

    results = finish([start(a, *build_args), start(b, *build_args)])  # both started before either is awaited
    for code, out, err in results:
        assert code == 0, (code, out[-2000:], err[-2000:])

    hashes = []
    for work in (a, b):
        marker = (work / "warehouse" / ".complete").read_text().strip()
        digest = rollforward.content_hash(WorkDir(work).warehouse_db)
        assert marker == digest, (work, marker, digest)
        hashes.append(digest)
    assert hashes[0] == hashes[1]

    # each build kept dbt's target/ and logs/ to its own work dir
    for work in (a, b):
        assert (work / "warehouse" / "dbt" / "target").is_dir(), work
        assert (work / "warehouse" / "dbt" / "logs").is_dir(), work
    assert repo_strays() == before, "a build wrote warehouse, dbt or site paths into the repository"

    for work in (a, b):
        check = subprocess.run(
            roi_args("memo", "--month", month, "--check", "--work-dir", str(work)),
            cwd=ROOT, capture_output=True, text=True, timeout=TIMEOUT,
        )
        assert check.returncode == 0, (work, check.stdout[-2000:], check.stderr[-2000:])
    assert (a / "site" / "index.html").read_bytes() == (b / "site" / "index.html").read_bytes()
    assert repo_strays() == before, "a memo wrote site paths into the repository"
