"""The scrub check: its shell tests pass and the tree itself is clean."""

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_scrub_check_shell_tests_pass():
    result = subprocess.run(
        ["bash", "tests/test_scrub_check.sh"], cwd=ROOT, capture_output=True, text=True, timeout=300
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_tree_has_no_secrets_or_private_paths():
    result = subprocess.run(
        ["scripts/scrub-check.sh", "--no-denylist"], cwd=ROOT, capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 0, result.stdout + result.stderr
