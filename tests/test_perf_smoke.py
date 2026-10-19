"""Performance smoke test: the Beta Co pipeline, timed step by step in one work dir.

Each step runs once (the budgets are stated at p95 over five runs; one run is the simplest reading)
and fails at twice its budget, so a breach shows in the printed table before it doubles. The verify
exit code depends on the registered draw (a planted effect may fall outside its interval), so
verify may exit 0 or 1 and only its time is measured. Peak memory is the high-water mark of the
resident set over every child process the steps started.
"""

import resource
import subprocess
import sys
import time
from pathlib import Path

import pytest

from roi import rollforward

pytestmark = pytest.mark.slow

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config" / "gen.yaml"
FAIL_FACTOR = 2
# Budgets in seconds; the build budget is dbt (60) + estimator (10) + the rest of the build.
BUDGETS = {
    "gen": 30.0,
    "build": 70.0,
    "estimate": 10.0,
    "verify": 20.0,
    "memo --check": 30.0,
    "hash guard": 3.0,
}
FULL_RUN_BUDGET = 180.0  # build + verify + memo
PEAK_RSS_BUDGET_MB = 1024.0
RUN = "import sys; from roi.cli import main; main(sys.argv[1:])"


def roi(*args, accept=(0,)):
    result = subprocess.run(
        [sys.executable, "-c", RUN, *args], cwd=ROOT, capture_output=True, text=True, timeout=600
    )
    assert result.returncode in accept, (args, result.returncode, result.stdout[-2000:], result.stderr[-2000:])


def timed(label, seconds, step, *args, **kwargs):
    start = time.monotonic()
    step(*args, **kwargs)
    seconds[label] = time.monotonic() - start


def children_peak_kb():
    return resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss  # kilobytes on Linux


def table(seconds, budgets, peak_mb):
    lines = [
        "perf smoke (one run each; fails at 2x budget)",
        f"  {'step':<14}{'measured s':>12}{'budget s':>10}{'fails at s':>12}",
    ]
    for label, budget in budgets.items():
        lines.append(f"  {label:<14}{seconds.get(label, float('nan')):>12.1f}{budget:>10.0f}{budget * FAIL_FACTOR:>12.0f}")
    lines.append(
        f"  {'peak RSS MB':<14}{peak_mb:>12.0f}{PEAK_RSS_BUDGET_MB:>10.0f}{PEAK_RSS_BUDGET_MB * FAIL_FACTOR:>12.0f}"
    )
    return "\n".join(lines)


def test_pipeline_steps_fit_their_budgets(tmp_path, request):
    work = tmp_path / "work"
    seconds: dict[str, float] = {}
    rss_before = children_peak_kb()
    try:
        timed("gen", seconds, roi, "gen", "--config", str(CONFIG), "--work-dir", str(work))
        timed("build", seconds, roi, "--work-dir", str(work), "build")
        timed("estimate", seconds, roi, "--work-dir", str(work), "estimate")
        timed("verify", seconds, roi, "--work-dir", str(work), "verify", "--through", "2026-03", accept=(0, 1))
        timed("memo --check", seconds, roi, "--work-dir", str(work), "memo", "--month", "2026-03", "--check")
        timed("hash guard", seconds, rollforward.content_hash, work / "warehouse" / "roi.duckdb")
    finally:
        rss_after = children_peak_kb()
        peak_mb = max(rss_after, rss_before) / 1024
        full_run = sum(seconds.get(k, 0.0) for k in ("build", "verify", "memo --check"))
        text = table(seconds, BUDGETS, peak_mb) + (
            f"\n  {'build+verify+memo':<18}{full_run:>8.1f}{FULL_RUN_BUDGET:>10.0f}{FULL_RUN_BUDGET * FAIL_FACTOR:>12.0f}"
        )
        reporter = request.config.pluginmanager.getplugin("terminalreporter")
        if reporter is not None:
            reporter.write_line("")
            for line in text.splitlines():
                reporter.write_line(line)
        else:
            print(text)

    breaches = {k: round(v, 1) for k, v in seconds.items() if v >= BUDGETS[k] * FAIL_FACTOR}
    assert not breaches, ("steps over twice their budget", breaches)
    assert full_run < FULL_RUN_BUDGET * FAIL_FACTOR, (full_run, FULL_RUN_BUDGET * FAIL_FACTOR)
    assert peak_mb < PEAK_RSS_BUDGET_MB * FAIL_FACTOR, (peak_mb, PEAK_RSS_BUDGET_MB * FAIL_FACTOR)
