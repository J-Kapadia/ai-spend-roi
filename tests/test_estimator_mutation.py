"""The estimator tests bite: one edited weight, divisor or control mean makes them fail.

Each test copies the package to a temporary directory, applies a single textual edit to the
copy's estimator, and runs tests/test_estimator.py in a subprocess with that copy first on
PYTHONPATH, so it shadows the editable install. The unmutated copy must pass, or a failing
mutant would prove nothing.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "roi"
ESTIMATOR_TESTS = "tests/test_estimator.py"


def _env(copy_root: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(copy_root), env.get("PYTHONPATH")]))
    return env


def _run_estimator_tests(copy_root: Path) -> int:
    """Exit code of test_estimator.py run against the package copy under copy_root."""
    env = _env(copy_root)
    where = subprocess.run(
        [sys.executable, "-c", "import roi.estimator, sys; print(roi.estimator.__file__)"],
        cwd=ROOT, env=env, capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert Path(where).resolve().is_relative_to(copy_root.resolve()), (
        f"the package copy does not shadow the installed one: roi.estimator is {where}"
    )
    done = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", ESTIMATOR_TESTS],
        cwd=ROOT, env=env, capture_output=True, text=True,
    )
    return done.returncode


def _mutant(tmp_path: Path, old: str, new: str) -> Path:
    """Copy src/roi to tmp_path/roi and replace the one occurrence of old in estimator.py."""
    shutil.copytree(SRC, tmp_path / "roi", ignore=shutil.ignore_patterns("__pycache__"))
    path = tmp_path / "roi" / "estimator.py"
    text = path.read_text()
    found = text.count(old)
    assert found == 1, f"mutation target {old!r} occurs {found} times in estimator.py, expected once"
    path.write_text(text.replace(old, new))
    return tmp_path


def test_unmutated_copy_passes(tmp_path):
    shutil.copytree(SRC, tmp_path / "roi", ignore=shutil.ignore_patterns("__pycache__"))
    assert _run_estimator_tests(tmp_path) == 0


@pytest.mark.parametrize(
    "name, old, new",
    [
        (
            "team-effect weights double the last cell",
            "w = np.full(len(cells), 1.0 / len(cells))",
            "w = np.full(len(cells), 1.0 / len(cells)); w[-1] *= 2",
        ),
        (
            "sample covariance divides by n",
            "/ (len(x) - 1)",
            "/ len(x)",
        ),
        (
            "backcast control mean drops one control",
            "_mean([panel.y[c][s] for c in controls])",
            "_mean([panel.y[c][s] for c in controls[1:]])",
        ),
    ],
    ids=["weights", "denominator", "control-mean"],
)
def test_mutation_is_caught(tmp_path, name, old, new):
    copy_root = _mutant(tmp_path, old, new)
    # exit 1 is "tests failed"; anything else (usage error, collection error) would not show the oracle bites
    assert _run_estimator_tests(copy_root) == 1, f"test_estimator.py does not fail on a test when {name}"
