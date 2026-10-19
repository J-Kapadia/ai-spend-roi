"""The Pages workflow deploys the toy company's site from every push to main, with no secret and locked dependencies."""

from pathlib import Path

import pytest
import yaml

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "pages.yml"


@pytest.fixture(scope="module")
def workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text())


def run_lines(workflow: dict) -> list[str]:
    lines = []
    for job in workflow["jobs"].values():
        for step in job["steps"]:
            lines.extend(l.strip() for l in step.get("run", "").splitlines() if l.strip())
    return lines


def step_uses(job: dict, action: str) -> list[dict]:
    return [s for s in job["steps"] if s.get("uses", "").startswith(action + "@")]


def test_triggers_are_push_to_main_and_manual_dispatch(workflow):
    # YAML 1.1 parses the bare key `on` as the boolean True.
    triggers = workflow.get("on", workflow.get(True))
    assert set(triggers) == {"push", "workflow_dispatch"}
    assert triggers["push"] == {"branches": ["main"]}


def test_every_uv_invocation_is_frozen(workflow):
    uv_lines = [l for l in run_lines(workflow) if "uv " in l]
    assert uv_lines
    for line in uv_lines:
        assert line.startswith(("uv sync --frozen", "uv run --frozen")), line


def test_build_checks_the_toy_company_memo_into_one_work_dir(workflow):
    lines = run_lines(workflow)
    build = [l for l in lines if "roi build --toy" in l]
    memo = [l for l in lines if "roi memo --month 2025-06 --check" in l]
    assert len(build) == 1 and len(memo) == 1
    work_dir = '--work-dir "$RUNNER_TEMP/work"'
    assert build[0].endswith(work_dir)
    assert memo[0].endswith(work_dir)


def test_upload_artifact_is_the_built_site(workflow):
    uploads = step_uses(workflow["jobs"]["build"], "actions/upload-pages-artifact")
    assert len(uploads) == 1
    assert uploads[0]["with"]["path"].endswith("work/site")


def test_deploy_follows_build_and_uses_the_pages_action(workflow):
    deploy = workflow["jobs"]["deploy"]
    needs = deploy["needs"]
    assert (needs if isinstance(needs, list) else [needs]) == ["build"]
    assert deploy["environment"]["name"] == "github-pages"
    assert len(step_uses(deploy, "actions/deploy-pages")) == 1


def test_permissions_are_exactly_the_pages_token_scopes(workflow):
    assert workflow["permissions"] == {"contents": "read", "pages": "write", "id-token": "write"}


def test_no_secret_is_referenced():
    assert "secrets." not in WORKFLOW.read_text()


def test_deploys_are_serialised_and_never_cancelled(workflow):
    assert workflow["concurrency"] == {"group": "pages", "cancel-in-progress": False}
