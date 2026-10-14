"""The `roi` entry point: a click group over lazily loaded command modules.

Commands are one module each in `roi.commands`. They are discovered with `pkgutil`
without importing and imported only when invoked, so a `roi build` process never loads
a module it does not need.
"""

from __future__ import annotations

import importlib
import pkgutil
from pathlib import Path

import click

from roi import commands
from roi.workdir import WorkDir


class LazyGroup(click.Group):
    def list_commands(self, ctx: click.Context) -> list[str]:
        return sorted(m.name for m in pkgutil.iter_modules(commands.__path__))

    def get_command(self, ctx: click.Context, cmd_name: str) -> click.Command | None:
        if cmd_name not in self.list_commands(ctx):
            return None
        return importlib.import_module(f"roi.commands.{cmd_name}").command


@click.group(cls=LazyGroup)
@click.option(
    "--work-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("."),
    show_default=True,
    help="Directory under which data/, truth/, warehouse/ and site/ resolve.",
)
@click.pass_context
def main(ctx: click.Context, work_dir: Path) -> None:
    """Tie AI spend, by team, to what each team produces."""
    ctx.obj = WorkDir(work_dir)
