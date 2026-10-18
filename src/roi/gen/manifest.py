"""The pinned hashes of the generated files: `sha256sum`-style lines, `<hex>  <path under data/>`.

`data/MANIFEST.sha256` lists every file under `books/`; the generator writes it and `roi build` checks
the books against it. `data/MANIFEST.incoming.sha256` lists every file of every incoming month; the
generator writes it and `roi rollforward` checks an incoming month against it before ingesting anything.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from roi.workdir import RoiError

BOOKS_NAME = "MANIFEST.sha256"
BOOKS_DIR = "books"
INCOMING_NAME = "MANIFEST.incoming.sha256"
INCOMING_DIR = "incoming"
INCOMING_FILES = ("raw_ai_spend", "raw_gl_journal", "raw_output", "raw_hr_roster")


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def incoming_path(month: str, name: str) -> str:
    """The manifest key of one incoming file: `incoming/<month>/<name>.csv`."""
    return f"{INCOMING_DIR}/{month}/{name}.csv"


def render(entries: dict[str, str]) -> str:
    return "".join(f"{digest}  {path}\n" for path, digest in sorted(entries.items()))


def parse(text: str) -> dict[str, str]:
    entries = {}
    for line in text.splitlines():
        if line.strip():
            digest, _, path = line.partition("  ")
            entries[path] = digest
    return entries


def write_incoming(data_dir: Path) -> dict[str, str]:
    """Hash every file under `incoming/` and write the manifest beside it."""
    entries = {
        path.relative_to(data_dir).as_posix(): sha256_file(path)
        for path in sorted((data_dir / INCOMING_DIR).glob("*/*.csv"))
    }
    (data_dir / INCOMING_NAME).write_text(render(entries), encoding="utf-8")
    return entries


def _books_entries(data_dir: Path) -> dict[str, str]:
    return {
        path.relative_to(data_dir).as_posix(): sha256_file(path)
        for path in sorted((data_dir / BOOKS_DIR).glob("*.csv"))
    }


def write_books(data_dir: Path) -> dict[str, str]:
    """Hash every `books/*.csv` and write the manifest beside the directory."""
    entries = _books_entries(data_dir)
    (data_dir / BOOKS_NAME).write_text(render(entries), encoding="utf-8")
    return entries


def check_books(data_dir: Path) -> None:
    """Exit 3 unless the books files are exactly those listed in the manifest, byte for byte."""
    path = data_dir / BOOKS_NAME
    if not path.exists():
        raise RoiError(3, f"{path} is missing; run `roi gen`")
    pinned = parse(path.read_text(encoding="utf-8"))
    actual = _books_entries(data_dir)
    for name, digest in pinned.items():
        if name not in actual:
            raise RoiError(3, f"{name} is listed in {path.name} but missing; run `roi gen`")
        if actual[name] != digest:
            raise RoiError(3, f"{name} differs from {path.name}; the books or the generator changed")
    for name in actual:
        if name not in pinned:
            raise RoiError(3, f"{name} is not listed in {path.name}")
