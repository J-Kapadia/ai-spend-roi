"""Layout rules: commands, steps and checks are registries of one module each."""

import ast
import importlib
import pkgutil
import subprocess
import sys
import tomllib
from pathlib import Path

import click
import yaml

import roi.checks
import roi.commands
import roi.steps
from roi.cli import main

ROOT = Path(__file__).resolve().parents[1]
COMMANDS = ROOT / "src" / "roi" / "commands"
CHECKS = ROOT / "src" / "roi" / "checks"


def command_names() -> list[str]:
    return sorted(m.name for m in pkgutil.iter_modules(roi.commands.__path__))


def test_every_command_module_registers():
    names = command_names()
    assert names, "no command modules found"
    assert main.list_commands(click.Context(main)) == names
    for name in names:
        cmd = importlib.import_module(f"roi.commands.{name}").command
        assert isinstance(cmd, click.Command)
        assert cmd.name == name


def test_no_command_module_imports_another():
    for path in sorted(COMMANDS.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom):
                imported = [("." * node.level) + (node.module or "")]
                if node.module == "roi" or node.level:
                    imported += [("." * node.level) + (node.module or "") + "." + a.name for a in node.names]
            elif isinstance(node, ast.Import):
                imported = [a.name for a in node.names]
            else:
                continue
            for name in imported:
                assert not name.startswith("roi.commands") and not name.startswith("."), f"{path.name} imports {name}"


def test_importing_the_cli_loads_no_command_module():
    code = "import sys, roi.cli; sys.exit(any(m.startswith('roi.commands.') for m in sys.modules))"
    assert subprocess.run([sys.executable, "-c", code]).returncode == 0


def test_steps_are_a_registry():
    steps = roi.steps.discover()
    assert steps
    assert [s.ORDER for s in steps] == sorted({s.ORDER for s in steps}), "step ORDER must be unique"
    assert len({s.NAME for s in steps}) == len(steps)
    for s in steps:
        assert callable(s.run)


def test_every_check_registers():
    files = sorted(p.stem for p in CHECKS.glob("*.py") if p.stem != "__init__")
    checks = roi.checks.discover()
    assert files and sorted(c.__name__.rsplit(".", 1)[1] for c in checks) == files
    assert [c.ORDER for c in checks] == sorted({c.ORDER for c in checks}), "check ORDER must be unique"
    assert len({c.NAME for c in checks}) == len(checks)
    for c in checks:
        assert callable(c.run)
        assert c.NAME == c.__name__.rsplit(".", 1)[1], "a check is named after its file"


def test_no_check_module_imports_another():
    for path in sorted(CHECKS.glob("*.py")):
        if path.name == "__init__.py":
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom):
                imported = [("." * node.level) + (node.module or "")]
                if node.module in ("roi", "roi.checks") or node.level:
                    imported += [imported[0] + "." + a.name for a in node.names]
            elif isinstance(node, ast.Import):
                imported = [a.name for a in node.names]
            else:
                continue
            for name in imported:
                assert name in ("roi.checks", "roi.checks.CheckResult") or not (
                    name.startswith("roi.checks.") or name.startswith(".")
                ), f"{path.name} imports {name}"


def test_entry_point_and_slow_marker():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert project["project"]["scripts"]["roi"] == "roi.cli:main"
    assert any(m.startswith("slow:") for m in project["tool"]["pytest"]["ini_options"]["markers"])


def test_dbt_sends_no_usage_stats():
    project = yaml.safe_load((ROOT / "dbt" / "dbt_project.yml").read_text())
    assert project["flags"]["send_anonymous_usage_stats"] is False
