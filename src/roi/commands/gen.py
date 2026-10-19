"""`roi gen`: generate the books and `truth/truth.json` from the seed."""

from __future__ import annotations

from pathlib import Path

import click

from roi.gen import books, config, manifest, panel, toy
from roi.output import Report, reporting
from roi.workdir import WorkDir


def _record(report: Report, work: WorkDir, detail: str) -> None:
    report.check("generated", True, detail)
    for path in sorted(work.books.glob("*.csv")):
        report.artifact(path)
    for name in (manifest.BOOKS_NAME, manifest.INCOMING_NAME):
        if (work.data / name).exists():
            report.artifact(work.data / name)
    report.artifact(work.truth / "truth.json")


@click.command("gen")
@click.option("--config", "config_path", type=click.Path(dir_okay=False, path_type=Path),
              default=Path("config/gen.yaml"), show_default=True, help="Generator config.")
@click.option("--work-dir", "work_dir", type=click.Path(file_okay=False, path_type=Path), default=None,
              help="Directory for data/ and truth/; overrides the global --work-dir.")
@click.option("--toy", "use_toy", is_flag=True, help="Generate the 3-team, 6-month toy company instead.")
@click.option("--json", "as_json", is_flag=True, help="Print one JSON object instead of text.")
@click.pass_obj
def command(work: WorkDir, config_path: Path, work_dir: Path | None, use_toy: bool, as_json: bool) -> None:
    """Generate the books and the truth file, deterministically from the config."""
    if work_dir is not None:
        work = WorkDir(work_dir)
    with reporting("gen", as_json) as report:
        if use_toy:
            toy.generate(work)
            click.echo(f"wrote toy books to {work.books}")
            _record(report, work, "toy books and truth written")
            return
        cfg = config.load(config_path)
        books.write(cfg, panel.simulate(cfg), work)
        note = " (fixture)" if cfg.fixture else ""
        click.echo(f"wrote books to {work.books} and truth to {work.truth / 'truth.json'}{note}")
        _record(report, work, "books and truth written" + note)
