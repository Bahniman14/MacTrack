from __future__ import annotations

import csv
from datetime import datetime, timezone

import pytest

import storage


def _row(item_id: str, price: int | None, **extra) -> dict:
    return {
        "timestamp_ist": storage.timestamp_ist(),
        "item_id": item_id,
        "line_item": "device",
        "seller": "apple_in_edu",
        "price_type": "education",
        "url": "https://example.invalid/p",
        "part_number_matched": "Z1KM",
        "mrp_inr": 267000,
        "price_inr": price,
        "in_stock": True,
        "offers_raw": None,
        "status": "ok",
        "error": None,
        **extra,
    }


def test_creates_file_with_header(tmp_path):
    path = tmp_path / "docs" / "data" / "prices.csv"

    storage.append_rows([_row("a", 255000)], path)

    with path.open(encoding="utf-8", newline="") as f:
        rows = list(csv.reader(f))
    assert tuple(rows[0]) == storage.COLUMNS
    assert len(rows) == 2


def test_append_is_append_only(tmp_path):
    path = tmp_path / "prices.csv"
    storage.append_rows([_row("a", 255000), _row("b", 26900)], path)
    first_bytes = path.read_bytes()

    storage.append_rows([_row("a", 251000, offers_raw="multi\nline, \"quoted\"")], path)

    after = path.read_bytes()
    assert after.startswith(first_bytes)
    with path.open(encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    assert [r["price_inr"] for r in rows] == ["255000", "26900", "251000"]
    assert after.count(b"timestamp_ist") == 1  # header written once


def test_value_formatting(tmp_path):
    path = tmp_path / "prices.csv"

    storage.append_rows([_row("a", None, in_stock=None, status="blocked", error="HTTP 403")], path)

    with path.open(encoding="utf-8", newline="") as f:
        row = next(csv.DictReader(f))
    assert row["price_inr"] == ""
    assert row["in_stock"] == ""
    assert row["status"] == "blocked"


def test_refuses_to_append_to_file_with_other_header(tmp_path):
    path = tmp_path / "prices.csv"
    path.write_text("a,b,c\n1,2,3\n", encoding="utf-8")

    with pytest.raises(ValueError):
        storage.append_rows([_row("a", 1)], path)
    assert path.read_text(encoding="utf-8") == "a,b,c\n1,2,3\n"


def test_rejects_unknown_columns(tmp_path):
    with pytest.raises(ValueError):
        storage.append_rows([_row("a", 1, bogus="x")], tmp_path / "prices.csv")


def test_ist_timestamp_ends_with_offset():
    ts = storage.timestamp_ist()

    assert ts.endswith("+05:30")
    assert datetime.fromisoformat(ts).utcoffset().total_seconds() == 5.5 * 3600


def test_timestamp_converts_utc_to_ist():
    utc = datetime(2026, 1, 1, 3, 30, tzinfo=timezone.utc)

    assert storage.timestamp_ist(utc) == "2026-01-01T09:00:00+05:30"


def test_now_ist_offset():
    assert storage.now_ist().utcoffset().total_seconds() == 5.5 * 3600
