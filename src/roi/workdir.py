"""State directories, all resolved from the global --work-dir (plan D-23)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import click


class RoiError(click.ClickException):
    """A failure with an exit code from the plan's error contract (§14)."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.exit_code = code

    def show(self, file=None) -> None:
        click.echo(f"error[{self.exit_code}]: {self.message}", err=True)


@dataclass(frozen=True)
class WorkDir:
    root: Path

    @property
    def data(self) -> Path:
        return self.root / "data"

    @property
    def books(self) -> Path:
        return self.data / "books"

    @property
    def truth(self) -> Path:
        return self.root / "truth"

    @property
    def warehouse(self) -> Path:
        return self.root / "warehouse"

    @property
    def warehouse_db(self) -> Path:
        return self.warehouse / "roi.duckdb"

    @property
    def warehouse_next_db(self) -> Path:
        return self.warehouse / "roi.next.duckdb"

    @property
    def site(self) -> Path:
        return self.root / "site"
