"""Build steps: a registry of one module per step (plan D-23).

A step module defines `NAME`, `ORDER` and `run(ctx)`. `roi build` runs them in ORDER.
Each step opens and closes its own warehouse connection, because dbt must run while
`roi` holds none.
"""

from __future__ import annotations

import importlib
import pkgutil
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

from roi.workdir import WorkDir


@dataclass(frozen=True)
class BuildContext:
    work: WorkDir
    db: Path  # the file being built: warehouse/roi.next.duckdb
    through: str  # last month of the books, YYYY-MM


def discover() -> list[ModuleType]:
    modules = [importlib.import_module(f"{__name__}.{m.name}") for m in pkgutil.iter_modules(__path__)]
    return sorted(modules, key=lambda m: m.ORDER)
