"""`data/reference/ramp_ai_index.csv` is a hand-checked table of published figures: its shape is fixed."""

import csv
import re
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TABLE = ROOT / "data" / "reference" / "ramp_ai_index.csv"
README = ROOT / "data" / "reference" / "README.md"
COLUMNS = ["metric", "period", "value", "unit", "source_url", "retrieved_on", "notes"]
PERIOD = re.compile(r"\d{4}-(0[1-9]|1[0-2])")
FIRST_PERIOD = "2023-01"


def read():
    with TABLE.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        return reader.fieldnames, list(reader)


def test_header_is_the_seven_columns():
    header, _ = read()
    assert header == COLUMNS


def test_metrics_are_ids_and_the_headline_is_present():
    _, rows = read()
    assert all(re.fullmatch(r"[a-z0-9_]+", row["metric"]) for row in rows)
    assert any(row["metric"] == "paid_adoption_share" for row in rows)


def test_period_is_a_month_between_2023_01_and_the_retrieval_month():
    _, rows = read()
    for row in rows:
        assert PERIOD.fullmatch(row["period"]), row
        assert FIRST_PERIOD <= row["period"] <= row["retrieved_on"][:7], row


def test_rows_are_unique_and_sorted_by_metric_then_period():
    _, rows = read()
    keys = [(row["metric"], row["period"]) for row in rows]
    assert len(keys) == len(set(keys))
    assert keys == sorted(keys)


def test_values_are_bounded_decimals_with_a_known_unit():
    _, rows = read()
    for row in rows:
        try:
            value = Decimal(row["value"])
        except InvalidOperation:
            raise AssertionError(f"not a decimal: {row}")
        assert value.is_finite() and value.as_tuple().exponent >= -4, row
        assert value >= 0, row
        assert row["unit"] in {"percent", "usd", "count"}, row
        if row["unit"] == "percent":
            assert value <= 100, row


def test_every_row_names_its_public_source_and_retrieval_date():
    _, rows = read()
    for row in rows:
        assert row["source_url"].startswith("https://") and "ramp.com" in row["source_url"], row
        assert date.fromisoformat(row["retrieved_on"]) <= date.today(), row
        assert row["notes"].strip(), row


def test_gitignore_tracks_the_reference_directory():
    assert "!/data/reference/" in (ROOT / ".gitignore").read_text().splitlines()


def test_readme_names_every_column():
    text = README.read_text()
    for column in COLUMNS:
        assert f"`{column}`" in text, column
