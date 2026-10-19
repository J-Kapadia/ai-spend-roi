"""Verification checks of group (c): a registry of one module per check.

A check module defines `NAME`, `ORDER` and `run(con, through, work) -> CheckResult`. `roi verify`
runs them in ORDER and prints their names. A check raises `RoiError(3, ...)` when a table it
needs is absent: a table that should have been built is a failure of the build, never a skip.
"""

from __future__ import annotations

import importlib
import pkgutil
from dataclasses import dataclass
from types import ModuleType


@dataclass(frozen=True)
class CheckResult:
    id: str
    failures: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()  # worth a line in the output, never a failed run
    note: str | None = None  # what the check did not do, or accepted, and why


def discover() -> list[ModuleType]:
    modules = [importlib.import_module(f"{__name__}.{m.name}") for m in pkgutil.iter_modules(__path__)]
    names = [m.NAME for m in modules]
    if len(set(names)) != len(names):
        raise RuntimeError(f"two checks share a name: {sorted(n for n in names if names.count(n) > 1)}")
    return sorted(modules, key=lambda m: m.ORDER)
