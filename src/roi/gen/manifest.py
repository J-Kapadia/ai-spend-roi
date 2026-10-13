"""The pinned hashes of the generated files: `sha256sum`-style lines, `<hex>  <path under data/>`.

`data/MANIFEST.incoming.sha256` lists every file of every incoming month. The generator writes it
and `roi rollforward` checks an incoming month against it before ingesting anything.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

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
