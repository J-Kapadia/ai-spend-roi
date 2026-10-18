"""`data/MANIFEST.sha256` pins the books: `roi gen` writes it, `roi build` checks it, the committed one is current."""

import hashlib
import json
import shutil
from pathlib import Path

import pytest
from click.testing import CliRunner

from roi.cli import main
from roi.gen import manifest

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "gen_fixture.yaml"
ROI_FIXTURE = ROOT / "tests" / "fixtures" / "roi_fixture.yaml"
REAL_CONFIG = ROOT / "config" / "gen.yaml"
COMMITTED = ROOT / "data" / manifest.BOOKS_NAME
BOOKS = ["books/dim_team.csv", "books/raw_ai_spend.csv", "books/raw_gl_journal.csv",
         "books/raw_hr_roster.csv", "books/raw_output.csv"]


def roi(work, *args):
    return CliRunner().invoke(main, ["--work-dir", str(work), *args])


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture(scope="module")
def generated(tmp_path_factory):
    work = tmp_path_factory.mktemp("generated")
    (work / "config").mkdir()
    shutil.copy(ROI_FIXTURE, work / "config" / "roi.yaml")
    result = roi(work, "gen", "--config", str(FIXTURE))
    assert result.exit_code == 0, result.output
    return work


@pytest.fixture
def work(generated, tmp_path):
    shutil.copytree(generated, tmp_path / "w")
    return tmp_path / "w"


def test_gen_writes_a_manifest_of_exactly_the_books_files(generated):
    path = generated / "data" / manifest.BOOKS_NAME
    pinned = manifest.parse(path.read_text(encoding="utf-8"))
    assert sorted(pinned) == BOOKS
    assert all(digest == sha(generated / "data" / name) for name, digest in pinned.items())
    assert all(line.count("  ") == 1 for line in path.read_text(encoding="utf-8").splitlines())


def test_build_accepts_the_books_it_generated_and_rejects_a_changed_file(work):
    built = roi(work, "build")
    assert built.exit_code == 0, built.output
    published = sha(work / "warehouse" / "roi.duckdb")

    journal = work / "data" / "books" / "raw_gl_journal.csv"
    journal.write_bytes(journal.read_bytes() + b"\n")
    result = roi(work, "build")
    assert result.exit_code == 3
    assert "books/raw_gl_journal.csv" in result.output
    assert sha(work / "warehouse" / "roi.duckdb") == published
    assert not (work / "warehouse" / "roi.next.duckdb").exists()


def test_build_rejects_a_missing_manifest(work):
    (work / "data" / manifest.BOOKS_NAME).unlink()
    result = roi(work, "build")
    assert result.exit_code == 3
    assert "MANIFEST.sha256 is missing" in result.output


def test_check_books_names_a_missing_file_and_an_unlisted_one(work):
    data = work / "data"
    manifest.check_books(data)
    extra = data / "books" / "extra.csv"
    extra.write_text("a\n", encoding="utf-8")
    with pytest.raises(manifest.RoiError, match="books/extra.csv is not listed"):
        manifest.check_books(data)
    extra.unlink()
    (data / "books" / "dim_team.csv").unlink()
    with pytest.raises(manifest.RoiError, match="books/dim_team.csv"):
        manifest.check_books(data)


def test_toy_build_writes_its_own_manifest(tmp_path):
    result = roi(tmp_path, "build", "--toy")
    assert result.exit_code == 0, result.output
    pinned = manifest.parse((tmp_path / "data" / manifest.BOOKS_NAME).read_text(encoding="utf-8"))
    assert sorted(pinned) == BOOKS


def test_committed_manifest_equals_a_fresh_generation(tmp_path):
    result = roi(tmp_path, "gen", "--config", str(REAL_CONFIG))
    assert result.exit_code == 0, result.output
    fresh = (tmp_path / "data" / manifest.BOOKS_NAME).read_text(encoding="utf-8")
    assert COMMITTED.read_text(encoding="utf-8") == fresh
    assert sorted(manifest.parse(fresh)) == BOOKS

    doctor = roi(tmp_path, "doctor", "--tree", "--json")
    books = next(c for c in json.loads(doctor.output)["checks"] if c["id"] == "books")
    assert books["pass"] and books["detail"].startswith("ok")
