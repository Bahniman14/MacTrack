"""Append-only CSV storage and IST time helper."""

from __future__ import annotations

import csv
from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    IST = ZoneInfo("Asia/Kolkata")
except (ImportError, ZoneInfoNotFoundError):  # tzdata missing (common on Windows)
    IST = timezone(timedelta(hours=5, minutes=30), "IST")

DEFAULT_CSV_PATH = Path(__file__).resolve().parent / "docs" / "data" / "prices.csv"

COLUMNS = (
    "timestamp_ist",
    "item_id",
    "line_item",
    "seller",
    "price_type",
    "url",
    "part_number_matched",
    "mrp_inr",
    "price_inr",
    "in_stock",
    "offers_raw",
    "status",
    "error",
)


def now_ist() -> datetime:
    return datetime.now(IST)


def timestamp_ist(dt: datetime | None = None) -> str:
    """ISO-8601 timestamp in IST, e.g. 2026-01-01T09:00:00+05:30."""
    return (dt or now_ist()).astimezone(IST).isoformat(timespec="seconds")


def append_rows(rows: Iterable[Mapping[str, Any]], csv_path: Path = DEFAULT_CSV_PATH) -> int:
    """Append rows to the CSV, writing the header only when the file is new. Returns rows written."""
    csv_path = Path(csv_path)
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    is_new = not csv_path.exists() or csv_path.stat().st_size == 0
    if not is_new:
        _check_header(csv_path)

    formatted = [_format_row(r) for r in rows]
    with csv_path.open("a", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS, extrasaction="raise")
        if is_new:
            writer.writeheader()
        writer.writerows(formatted)
    return len(formatted)


def _check_header(csv_path: Path) -> None:
    with csv_path.open("r", encoding="utf-8", newline="") as f:
        header = next(csv.reader(f), [])
    if tuple(header) != COLUMNS:
        raise ValueError(f"{csv_path} has unexpected header {header}; refusing to append")


def _format_row(row: Mapping[str, Any]) -> dict[str, str]:
    unknown = set(row) - set(COLUMNS)
    if unknown:
        raise ValueError(f"unknown CSV columns: {sorted(unknown)}")
    return {col: _format_value(row.get(col)) for col in COLUMNS}


def _format_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return " ".join(str(value).split())  # keep each record on one physical line
