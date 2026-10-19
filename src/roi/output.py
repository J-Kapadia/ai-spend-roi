"""The machine-readable result of a command: one JSON object on stdout with `--json`.

A command records what it checked with `Report.check` and what it wrote with `Report.artifact`.
`reporting` wraps the command body: with `--json` it keeps stdout for the one object (the text the
command prints as it goes moves to stderr), turns a `RoiError` into a failed check with id `error`,
and re-raises it so the exit code is the one of the error contract. Without `--json` it does nothing.
"""

from __future__ import annotations

import contextlib
import json
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import click

from roi.workdir import RoiError


@dataclass
class Report:
    command: str
    checks: list[dict] = field(default_factory=list)
    artifacts: list[str] = field(default_factory=list)
    errored: bool = False

    def check(self, id: str, passed: bool, detail: str = "") -> None:
        self.checks.append({"id": id, "pass": bool(passed), "detail": detail})

    def artifact(self, path: Path | str) -> None:
        self.artifacts.append(str(path))

    @property
    def status(self) -> str:
        if self.errored:
            return "error"
        return "fail" if any(not c["pass"] for c in self.checks) else "pass"

    def emit(self, as_json: bool) -> None:
        """Print the JSON object; in text mode print nothing, the command has printed as it went."""
        if as_json:
            click.echo(json.dumps({
                "command": self.command, "status": self.status,
                "checks": self.checks, "artifacts": self.artifacts,
            }, indent=2))


@contextlib.contextmanager
def reporting(command: str, as_json: bool) -> Iterator[Report]:
    report = Report(command)
    if not as_json:
        yield report
        return
    try:
        with contextlib.redirect_stdout(sys.stderr):
            yield report
    except RoiError as e:
        report.errored = True
        report.check("error", False, e.message)
        report.emit(True)
        raise
    report.emit(True)
