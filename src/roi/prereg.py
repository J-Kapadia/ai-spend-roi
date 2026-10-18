"""The pre-registration: a hash over the registered inputs, and the log of every registration.

`config/prereg.sha256` holds the SHA-256 of the registered inputs: the whole of `config/gen.yaml`
(seed, DGP keys, price table, the Beta Co table with its planted effects and noise), the effect
ranges of `config/calibration.yaml`, and the interval level and pre-trend level of the
estimator. `config/prereg.log` has one line per registration. The only sanctioned way to change
the seed afterwards is `reregister`, which has a budget per estimator version. A new seed changes the
books, so `reregister` regenerates them into a temporary directory and rewrites `data/MANIFEST.sha256`
and `data/MANIFEST.incoming.sha256` in the same step. `reregister --dgp-change` is the second form: the
seed stays and a data-generating key (ramp, noise, ...) edited by hand is registered, at the same cost.

Run as `python -m roi.prereg {hash,check,reregister}`; `scripts/reregister-seed.sh` wraps the last.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import tempfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import click
import yaml

from roi import calibration

REPO = Path(__file__).resolve().parents[2]
MAX_REGISTRATIONS = 4  # per estimator version: the first and three re-registrations
SHA = re.compile(r"[0-9a-f]{64}")
FIELD = re.compile(r'(\w+)=(?:"((?:[^"\\]|\\.)*)"|(\S*))')

EXIT_USAGE, EXIT_REFUSED = 2, 3


class Refused(Exception):
    """A registration that must not happen (exit 3); the message says why."""


# ---- the hash --------------------------------------------------------------------------------------

def thresholds() -> dict[str, float]:
    from roi import estimator  # imported late: the hash needs the current constants, not the import cost

    return {"level": estimator.LEVEL, "pretrend_alpha": estimator.PRETREND_ALPHA}


def registered_inputs(gen: Path, calib: Path) -> dict:
    """The parsed registered inputs. Comments, whitespace and key order in the files are not inputs."""
    cal = calibration.read(calib)
    ranges = {r["team_id"]: [r["effect_low"], r["effect_high"]] for r in cal.get("rows") or []}
    return {"gen": yaml.safe_load(gen.read_text(encoding="utf-8")), "calibration_ranges": ranges, "thresholds": thresholds()}


def _digest(value: object) -> str:
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def compute_hash(gen: Path, calib: Path) -> str:
    return _digest(registered_inputs(gen, calib))


def dgp_hash(gen: Path) -> str:
    """The hash of the data-generating keys: everything in gen.yaml the registration covers except the seed."""
    raw = dict(yaml.safe_load(gen.read_text(encoding="utf-8")) or {})
    raw.pop("seed", None)
    return _digest(raw)


# ---- the log ---------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Entry:
    date: str
    estimator: str
    seed: int
    sha256: str
    old_seed: int | None
    why: str
    dgp: str | None = None  # hash of the data-generating keys after this registration; absent in older lines
    old_dgp: str | None = None  # the same hash before it; set only by a DGP change


def format_entry(e: Entry) -> str:
    """One line: `<date> estimator=.. seed=.. sha256=.. [old_seed=..] [dgp=.. [old_dgp=..]] why="..."`, newlines folded."""
    why = " ".join(e.why.split()).replace("\\", "\\\\").replace('"', '\\"')
    old = "" if e.old_seed is None else f" old_seed={e.old_seed}"
    dgp = "" if e.dgp is None else f" dgp={e.dgp}"
    old_dgp = "" if e.old_dgp is None else f" old_dgp={e.old_dgp}"
    return f'{e.date} estimator={e.estimator} seed={e.seed} sha256={e.sha256}{old}{dgp}{old_dgp} why="{why}"'


def parse_entry(line: str) -> Entry:
    day, _, rest = line.partition(" ")
    fields = {m[1]: m[2] if m[2] or not m[3] else m[3] for m in FIELD.finditer(rest)}
    fields = {k: (re.sub(r"\\(.)", r"\1", v) if k == "why" else v) for k, v in fields.items()}
    try:
        date.fromisoformat(day)
        entry = Entry(day, fields["estimator"], int(fields["seed"]), fields["sha256"],
                      int(fields["old_seed"]) if "old_seed" in fields else None, fields.get("why", ""),
                      fields.get("dgp"), fields.get("old_dgp"))
    except (KeyError, ValueError) as e:
        raise ValueError(f"not a prereg.log entry: {line!r}") from e
    if not all(h is None or SHA.fullmatch(h) for h in (entry.sha256, entry.dgp, entry.old_dgp)):
        raise ValueError(f"not a prereg.log entry (bad sha256): {line!r}")
    return entry


def read_log(path: Path) -> list[Entry]:
    if not path.exists():
        return []
    return [parse_entry(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def append_only(old: str, new: str) -> bool:
    """True when `new` is `old` with lines added at the end and nothing else changed."""
    return new.startswith(old) and (not old or old.endswith("\n"))


def count_for(entries: list[Entry], estimator: str) -> int:
    """Draws spent on this estimator version. A version whose own log lines do not begin with the
    initial registration met the log with a seed already registered, and that seed was its first draw."""
    own = [e for e in entries if e.estimator == estimator]
    return len(own) + (0 if any(e.old_seed is None for e in own) else 1)


# ---- the registration ------------------------------------------------------------------------------

@dataclass(frozen=True)
class Paths:
    gen: Path
    calib: Path
    digest: Path
    log: Path

    @property
    def data(self) -> Path:
        return self.gen.parent.parent / "data"

    @classmethod
    def under(cls, root: Path) -> Paths:
        c = root / "config"
        return cls(c / "gen.yaml", c / "calibration.yaml", c / "prereg.sha256", c / "prereg.log")


def estimator_version() -> str:
    from roi import estimator

    return estimator.ESTIMATOR_VERSION


def problems(paths: Paths, estimator: str | None = None) -> list[str]:
    """What is wrong with the registration on disk; empty when the hash file, the inputs and the log agree."""
    if not paths.digest.exists():
        return [f"{paths.digest} does not exist"]
    found: list[str] = []
    try:
        entries = read_log(paths.log)
    except ValueError as e:
        return [str(e)]
    recorded = paths.digest.read_text(encoding="utf-8").strip()
    if not entries:
        return [f"{paths.log} has no entries"]
    if recorded != entries[-1].sha256:
        found.append("the hash file is not the hash of the last log entry")
    if recorded != compute_hash(paths.gen, paths.calib):
        found.append("a registered input has changed since it was registered")
    seed = (yaml.safe_load(paths.gen.read_text(encoding="utf-8")) or {}).get("seed")
    if seed != entries[-1].seed:
        found.append(f"the seed in gen.yaml ({seed}) is not the last registered seed ({entries[-1].seed})")
    for version in sorted({e.estimator for e in entries}):
        if count_for(entries, version) > MAX_REGISTRATIONS:
            found.append(f"estimator {version} has more than {MAX_REGISTRATIONS} registrations")
    return found


def register_initial(paths: Paths, *, estimator: str, day: str) -> Entry:
    """The first registration: write the hash and the first log line. Refuses when a log exists."""
    if paths.log.exists() and read_log(paths.log):
        raise Refused(f"{paths.log} already has entries; use reregister")
    seed = int((yaml.safe_load(paths.gen.read_text(encoding="utf-8")) or {})["seed"])
    entry = Entry(day, estimator, seed, compute_hash(paths.gen, paths.calib), None, "initial registration",
                  dgp_hash(paths.gen))
    _write(paths, paths.gen.read_text(encoding="utf-8"), entry, "", [])
    return entry


SEED_LINE = re.compile(r"^seed:[ \t]*\d+[ \t]*(#.*)?$", re.MULTILINE)


def _check_budget(entries: list[Entry], estimator: str) -> None:
    used = count_for(entries, estimator)
    if used >= MAX_REGISTRATIONS:
        raise Refused(
            f"the budget is spent: {used} registrations for estimator {estimator} "
            f"(the first and {MAX_REGISTRATIONS - 1} re-registrations); a further miss is a defect of the method"
        )


def _manifests(gen: Path) -> list[tuple[str, str]]:
    """The books manifest and the incoming manifest `roi gen` writes for this config, generated in a temp directory."""
    from roi.gen import books, config, manifest, panel  # imported late: only a registration needs numpy
    from roi.workdir import RoiError, WorkDir

    try:
        cfg = config.load(gen)
        with tempfile.TemporaryDirectory(prefix="roi-prereg-") as tmp:
            work = WorkDir(Path(tmp))
            books.write(cfg, panel.simulate(cfg), work)
            return [(name, (work.data / name).read_text(encoding="utf-8"))
                    for name in (manifest.BOOKS_NAME, manifest.INCOMING_NAME)]
    except RoiError as e:
        raise Refused(f"the books cannot be generated from {gen.name}: {e.message}") from e


def reregister(paths: Paths, new_seed: int, why: str, *, estimator: str, day: str) -> Entry:
    """Change the seed in gen.yaml, rewrite the hash file, the manifests and the log, or raise Refused.

    Nothing on disk changes when it refuses. It changes the seed and nothing else: a registered
    input edited by hand since the last registration is refused, and so is a spent budget. The
    books are regenerated at the new seed into a temporary directory and the two manifests under
    `data/` are replaced by theirs.
    """
    if new_seed <= 0:
        raise Refused("the new seed must be a positive integer")
    if not why.strip():
        raise Refused("a reason is required")
    entries = read_log(paths.log)
    if not entries:
        raise Refused(f"{paths.log} has no first registration to follow")
    bad = problems(paths)
    if bad:
        raise Refused("the current registration is not intact: " + "; ".join(bad))
    last = entries[-1]
    if new_seed == last.seed:
        raise Refused(f"{new_seed} is already the registered seed")
    _check_budget(entries, estimator)
    text = paths.gen.read_text(encoding="utf-8")
    if len(SEED_LINE.findall(text)) != 1:
        raise Refused(f"{paths.gen} must have exactly one top-level `seed: <integer>` line")
    new_text = SEED_LINE.sub(f"seed: {new_seed}", text)
    scratch = paths.gen.with_name(paths.gen.name + ".next")
    scratch.write_text(new_text, encoding="utf-8")
    try:
        digest = compute_hash(scratch, paths.calib)
        books = _manifests(scratch)
    finally:
        scratch.unlink()
    entry = Entry(day, estimator, new_seed, digest, last.seed, why, dgp_hash(paths.gen))
    _write(paths, new_text, entry, paths.log.read_text(encoding="utf-8"), books)
    return entry


def dgp_change(paths: Paths, why: str, *, estimator: str, day: str) -> Entry:
    """Register a data-generating key edited by hand since the last registration, keeping the seed.

    The log entry carries the hash of the data-generating keys before and after (the before is
    omitted when the last entry predates the field). The books are regenerated and the two manifests
    rewritten as `reregister` does. Refuses when nothing changed, when the seed was touched, or
    when the budget is spent; nothing on disk changes then.
    """
    if not why.strip():
        raise Refused("a reason is required")
    entries = read_log(paths.log)
    if not entries:
        raise Refused(f"{paths.log} has no first registration to follow")
    bad = [p for p in problems(paths) if "registered input has changed" not in p]
    if bad:
        raise Refused("the current registration is not intact: " + "; ".join(bad))
    last = entries[-1]
    digest = compute_hash(paths.gen, paths.calib)
    new_dgp = dgp_hash(paths.gen)
    if digest == last.sha256:
        raise Refused("nothing changed: gen.yaml and calibration.yaml are as registered")
    if new_dgp == last.dgp:
        raise Refused("no data-generating key changed; only registered inputs outside them did")
    _check_budget(entries, estimator)
    books = _manifests(paths.gen)
    entry = Entry(day, estimator, last.seed, digest, last.seed, why, new_dgp, last.dgp)
    _write(paths, paths.gen.read_text(encoding="utf-8"), entry, paths.log.read_text(encoding="utf-8"), books)
    return entry


def _write(paths: Paths, gen_text: str, entry: Entry, old_log: str, manifests: list[tuple[str, str]]) -> None:
    """Replace the manifests and the three config files, each through a temporary file, the log last so a
    half-write fails `problems`."""
    log = old_log + ("" if not old_log or old_log.endswith("\n") else "\n") + format_entry(entry) + "\n"
    paths.data.mkdir(parents=True, exist_ok=True)
    staged = [(paths.data / name, text) for name, text in manifests]
    staged += [(paths.gen, gen_text), (paths.digest, entry.sha256 + "\n"), (paths.log, log)]
    temps = []
    try:
        for target, text in staged:
            tmp = target.with_name(target.name + ".tmp")
            tmp.write_text(text, encoding="utf-8")
            temps.append((tmp, target))
        for tmp, target in temps:
            tmp.replace(target)
    finally:
        for tmp, _ in temps:
            tmp.unlink(missing_ok=True)


# ---- command line ----------------------------------------------------------------------------------

@click.group()
@click.option("--root", type=click.Path(file_okay=False, path_type=Path), default=REPO, show_default=True,
              help="Directory holding config/.")
@click.pass_context
def main(ctx: click.Context, root: Path) -> None:
    """Pre-registration hash and log."""
    ctx.obj = Paths.under(root)


@main.command("hash")
@click.pass_obj
def hash_cmd(paths: Paths) -> None:
    """Print the hash of the registered inputs as they are now."""
    click.echo(compute_hash(paths.gen, paths.calib))


@main.command("check")
@click.pass_obj
def check_cmd(paths: Paths) -> None:
    """Exit 1 unless the hash file, the inputs and the log agree."""
    found = problems(paths)
    for line in found:
        click.echo(f"prereg: {line}", err=True)
    sys.exit(1 if found else 0)


@main.command("reregister")
@click.option("--new-seed", type=int, help="Replace the seed.")
@click.option("--dgp-change", "dgp", is_flag=True,
              help="Keep the seed and register a data-generating key (ramp, noise, ...) edited by hand.")
@click.option("--why", required=True, help="Old seed, failing team and numbers, coverage miss or pre-trend flag.")
@click.pass_obj
def reregister_cmd(paths: Paths, new_seed: int | None, dgp: bool, why: str) -> None:
    """Re-register the seed or a change to the data-generating keys, and the manifests of the new books.

    Creates no tag: a signed prereg-v<n> tag is the author's to create."""
    if (new_seed is not None) == dgp:
        raise click.UsageError("give exactly one of --new-seed and --dgp-change")
    day = date.today().isoformat()
    try:
        if dgp:
            entry = dgp_change(paths, why, estimator=estimator_version(), day=day)
        else:
            entry = reregister(paths, new_seed, why, estimator=estimator_version(), day=day)
    except Refused as e:
        click.echo(f"reregister-seed: refused: {e}", err=True)
        sys.exit(EXIT_REFUSED)
    if entry.old_seed == entry.seed:
        click.echo(f"registered a data-generating change at seed {entry.seed}, sha256 {entry.sha256}")
    else:
        click.echo(f"registered seed {entry.seed} (was {entry.old_seed}), sha256 {entry.sha256}")
    click.echo("rewrote data/MANIFEST.sha256 and data/MANIFEST.incoming.sha256")
    click.echo("no tag was created; the author creates the signed tag prereg-v<n> by hand")


if __name__ == "__main__":
    main()
