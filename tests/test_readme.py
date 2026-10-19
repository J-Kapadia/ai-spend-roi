"""The README states what the method can and cannot show, and names only commands that exist."""

import re
import subprocess
from pathlib import Path

from roi.cli import main

ROOT = Path(__file__).resolve().parents[1]
README = (ROOT / "README.md").read_text()


def test_curve_identification_is_stated():
    assert "identified from the team's own spend ramp alone" in README
    assert "a learning curve cannot be told from diminishing returns" in README


def test_allocation_is_bounded_extrapolation():
    assert "bounded extrapolation" in README
    assert "50% beyond the highest monthly spend observed" in README
    assert "share of each increment above that highest observed spend" in README


def test_memo_prints_registration_count():
    assert "number of seed registrations" in README


def test_dbt_command_and_json_flag_are_documented():
    assert "`roi dbt <args>`" in README
    assert "`--json`" in README


def test_memo_renders_one_page_per_team():
    assert "one page per team, `site/teams/<team_id>.html`" in README
    assert "fails naming the page a failing number came from" in README
    assert (ROOT / "src" / "roi" / "templates" / "team.html.j2").is_file()


def test_memo_caveats_are_stated():
    for phrase in ("the number of seed registrations so far",
                   "a glossary of the pre-trend test",
                   "`no spend` where a window has none",
                   "`curve not identified; experiment before budget`",
                   "a note column on each designed experiment",
                   "the share of company spend that has no owning team",
                   "with a banner when that share is above 5%"):
        assert phrase in README, phrase


def test_public_statements_are_quoted_only_from_a_verified_citation_row():
    assert "quoted only from a row of `config/citations.yaml` that carries a verification date" in README
    assert (ROOT / "config" / "citations.yaml").is_file()


def test_offline_ci_build_is_stated_and_the_workflow_does_it():
    assert "inside a network namespace where the runner allows one" in README
    assert "unshare -n" in (ROOT / ".github" / "workflows" / "ci.yml").read_text()


def test_every_test_file_the_readme_names_exists():
    named = set(re.findall(r"`(tests/test_[a-z0-9_]+\.py)`", README))
    assert {"tests/test_workflows.py", "tests/test_concurrent_builds.py", "tests/test_benchmark_e2e.py",
            "tests/test_estimator_mutation.py", "tests/test_perf_smoke.py", "tests/test_scenario_08_tie_break.py",
            "tests/test_citations.py"} <= named
    assert not [n for n in named if not (ROOT / n).is_file()]


def test_no_private_process_strings_are_tracked():
    # Assembled from pieces so that this file does not match its own scan.
    pattern = "|".join(["docs/" + "plan", "docs/" + "build-log", "AGENTS" + r"\.md", r"\." + "beads", "Bead" + "-Id", "NEE" + "DLE"])
    tracked = subprocess.run(["git", "ls-files", "README.md", "src", "dbt", "config", ".github", "tests/*.py"],
                             cwd=ROOT, capture_output=True, text=True)
    files = tracked.stdout.split()
    if not files:  # a bare extraction has no .git: scan the same paths on disk
        paths = [ROOT / "README.md", *(ROOT / d for d in ("src", "dbt", "config", ".github")), *ROOT.glob("tests/*.py")]
        files = [str(f.relative_to(ROOT)) for p in paths for f in ([p] if p.is_file() else p.rglob("*") if p.exists() else [])
                 if f.is_file() and "__pycache__" not in f.parts]
    hits = []
    for name in files:
        try:
            text = (ROOT / name).read_text()
        except (UnicodeDecodeError, OSError):
            continue
        hits += [f"{name}:{n}" for n, line in enumerate(text.splitlines(), 1) if re.search(pattern, line)]
    assert not hits, f"private process strings in {hits}"


def test_every_readme_command_is_registered():
    registered = set(main.list_commands(None))
    named = set(re.findall(r"uv run roi ([a-z][a-z-]*)", README)) | set(re.findall(r"`roi ([a-z][a-z-]*)", README))
    assert named, "the README names no commands"
    assert named <= registered, f"not registered: {sorted(named - registered)}"
