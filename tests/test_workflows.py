"""The CI workflow tests offline, builds the synthetic company and runs the default tier of the definition of done."""

import re
from pathlib import Path

import pytest
import yaml

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ci.yml"


@pytest.fixture(scope="module")
def workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text())


def run_lines(job: dict) -> list[str]:
    lines = []
    for step in job["steps"]:
        lines.extend(l.strip() for l in step.get("run", "").splitlines() if l.strip())
    return lines


def all_run_lines(workflow: dict) -> list[str]:
    return [l for job in workflow["jobs"].values() for l in run_lines(job)]


def test_triggers_are_push_to_main_and_pull_request(workflow):
    # YAML 1.1 parses the bare key `on` as the boolean True.
    triggers = workflow.get("on", workflow.get(True))
    assert set(triggers) == {"push", "pull_request"}
    assert triggers["push"] == {"branches": ["main"]}


def test_permissions_are_read_only(workflow):
    assert workflow["permissions"] == {"contents": "read"}


def test_the_two_jobs_exist(workflow):
    assert set(workflow["jobs"]) == {"tests", "definition-of-done"}


def test_every_uv_invocation_is_frozen(workflow):
    uv_lines = [l for l in all_run_lines(workflow) if re.search(r"\buv\b", l)]
    assert uv_lines
    for line in uv_lines:
        command = line.removeprefix("$run ")
        assert command.startswith(("uv sync --frozen", "uv run --frozen")), line


def test_tests_job_runs_without_a_network_where_available(workflow):
    lines = run_lines(workflow["jobs"]["tests"])
    probe = [l for l in lines if l.startswith("run=") and "unshare -n true" in l]
    assert probe
    runs = [l for l in lines if l.startswith("$run uv run --frozen")]
    assert any("pytest -q -m \"not slow\"" in l for l in runs)
    for sub in ("roi gen", "roi build", "roi memo", "roi doctor --tree"):
        assert any(sub in l for l in runs), sub


def test_tests_job_builds_the_synthetic_company_in_one_temp_work_dir(workflow):
    lines = run_lines(workflow["jobs"]["tests"])
    assert any(l.startswith('work="$(mktemp -d)"') for l in lines)
    for sub in ("roi gen", "roi build", "roi memo", "roi doctor --tree"):
        matching = [l for l in lines if sub in l]
        assert len(matching) == 1, sub
        assert matching[0].endswith('--work-dir "$work"'), matching[0]


def test_memo_month_is_checked(workflow):
    memo = [l for l in run_lines(workflow["jobs"]["tests"]) if "roi memo" in l]
    assert len(memo) == 1
    assert "--month 2026-03" in memo[0]
    assert "--check" in memo[0].split()


def test_the_toy_company_is_not_used():
    assert "--toy" not in WORKFLOW.read_text()


def test_no_secret_is_referenced():
    assert "secrets." not in WORKFLOW.read_text()


def test_definition_of_done_job_runs_the_default_tier(workflow):
    job = workflow["jobs"]["definition-of-done"]
    assert job["timeout-minutes"] == 90
    script = [l for l in run_lines(job) if "definition-of-done.sh" in l]
    assert script == ["scripts/definition-of-done.sh"]
    assert "--fast" not in WORKFLOW.read_text()
