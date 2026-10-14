"""Sign-off: a human's attestation that a month's memo numbers were reviewed.

`signoffs/<month>.yaml` holds the reviewer's name, the date, the content hash of the built
site and the provenance ids of the numbers reviewed. The file by itself proves nothing, because
anyone can write it. The control is the commit that adds it: it must be signed (SSH) by a key in
`config/allowed-signers`, and when hardware keys are required every key in that file must be a
hardware-backed `sk-` type. Verification needs `.git`.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import yaml

from roi.workdir import RoiError, WorkDir

SIGNOFFS = "signoffs"
HARDWARE = "sk-"
KEY_PREFIXES = ("ssh-", "ecdsa-", "sk-")


def path(work: WorkDir, month: str) -> Path:
    return work.root / SIGNOFFS / f"{month}.yaml"


def signers_path(work: WorkDir) -> Path:
    return work.root / "config" / "allowed-signers"


def memo_hash(site: Path) -> str:
    """SHA-256 over every file of the built site: relative path, then bytes, in path order."""
    h = hashlib.sha256()
    for f in sorted(p for p in site.rglob("*") if p.is_file()):
        h.update(f.relative_to(site).as_posix().encode() + b"\0" + f.read_bytes() + b"\0")
    return h.hexdigest()


def load(work: WorkDir, month: str) -> dict | None:
    f = path(work, month)
    if not f.exists():
        return None
    data = yaml.safe_load(f.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def state(work: WorkDir, month: str) -> str:
    """One word for the memo check: a pending sign-off is allowed, a stale one is reported."""
    rec = load(work, month)
    if rec is None:
        return "pending (no signoffs/%s.yaml)" % month
    if (work.site / "index.html").exists() and rec.get("memo_sha256") != memo_hash(work.site):
        return f"stale, the memo has changed since {rec.get('name', 'the reviewer')} signed it"
    return f"recorded by {rec.get('name', '?')} on {rec.get('date', '?')}"


def write(work: WorkDir, month: str, name: str, today: str, prov_ids: list[str]) -> Path:
    """Write signoffs/<month>.yaml for the site as built now. Refuses to replace one."""
    f = path(work, month)
    if f.exists():
        raise RoiError(2, f"{f} already exists; a sign-off is never rewritten")
    if not (work.site / "index.html").exists():
        raise RoiError(2, f"no memo at {work.site}; run `roi memo --month {month}` first")
    f.parent.mkdir(parents=True, exist_ok=True)
    ids = "".join(f"  - {i}\n" for i in sorted(set(prov_ids)))
    f.write_text(
        f"month: '{month}'\nname: {json.dumps(name)}\ndate: '{today}'\n"
        f"memo_sha256: {memo_hash(work.site)}\nprov_ids:\n{ids}",
        encoding="utf-8",
    )
    return f


def key_types(signers: Path) -> list[str]:
    """The key type of every key in an allowed-signers file."""
    types = []
    for line in signers.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        found = [tok for tok in line.split() if tok.startswith(KEY_PREFIXES)]
        if found:
            types.append(found[0])
    return types


def hardware_problems(signers: Path) -> list[str]:
    if not signers.exists():
        return [f"{signers} does not exist"]
    types = key_types(signers)
    if not types:
        return [f"{signers} lists no keys"]
    return [f"{signers} lists a key that is not hardware-backed ({t})" for t in types if not t.startswith(HARDWARE)]


def _git(root: Path, *args: str, config: tuple[str, ...] = ()) -> subprocess.CompletedProcess:
    cmd = ["git", "-C", str(root)]
    for c in config:
        cmd += ["-c", c]
    return subprocess.run([*cmd, *args], capture_output=True, text=True, check=False)


def in_git(root: Path) -> bool:
    return _git(root, "rev-parse", "--git-dir").returncode == 0


def verify(work: WorkDir, month: str, signers: Path | None = None, require_hardware: bool = True) -> list[str]:
    """Problems with the sign-off of `month`; empty when it is recorded, current and signed.

    Raises RoiError(4) when there is no .git to check the signature against.
    """
    rec = load(work, month)
    if rec is None:
        return [f"no sign-off for {month}: signoffs/{month}.yaml is missing"]
    problems = []
    if not (work.site / "index.html").exists():
        problems.append(f"no memo at {work.site} to compare the sign-off with")
    elif rec.get("memo_sha256") != memo_hash(work.site):
        problems.append(f"the memo has changed since the sign-off of {month} was recorded")
    signers = signers or signers_path(work)
    if require_hardware:
        problems += hardware_problems(signers)
    elif not signers.exists():
        problems.append(f"{signers} does not exist")
    if problems:
        return problems
    if not in_git(work.root):
        raise RoiError(4, f"cannot verify the signature of the {month} sign-off without .git in {work.root}")
    rel = f"{SIGNOFFS}/{month}.yaml"
    added = _git(work.root, "log", "--diff-filter=A", "--format=%H", "--", rel).stdout.split()
    if not added:
        return [f"{rel} is not committed"]
    commit = added[-1]
    if _git(work.root, "show", f"{commit}:{rel}").stdout != path(work, month).read_text(encoding="utf-8"):
        return [f"{rel} differs from the file in the commit that added it"]
    signed = _git(work.root, "verify-commit", commit,
                  config=("gpg.format=ssh", f"gpg.ssh.allowedSignersFile={signers.resolve()}"))
    if signed.returncode != 0:
        return [f"commit {commit[:12]} that added {rel} is not signed by a key in {signers.name}"]
    return []
