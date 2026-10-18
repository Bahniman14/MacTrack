"""Plausibility checks for parsed prices.

A price that parsed cleanly can still be wrong (odd JSON-LD, a different plan, a placeholder).
Such rows are recorded with status "suspect": kept for transparency, but never used as a price
by alerts or the dashboard, and they count toward the failure streak.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterable, Mapping
from typing import Any

import parser as price_parser

MAX_CHANGE_RATIO = 0.25  # more than 25% away from the previous ok price is implausible


def previous_ok_prices(rows: Iterable[Mapping[str, str]]) -> dict[str, int]:
    """Latest ok price per item_id, in CSV (chronological) order."""
    latest: dict[str, int] = {}
    for row in rows:
        price = (row.get("price_inr") or "").strip()
        if row.get("status") == price_parser.STATUS_OK and price.isdigit():
            latest[row.get("item_id", "")] = int(price)
    return latest


def suspect_reason(price: int, item: Mapping[str, Any], previous_ok: int | None) -> str | None:
    """Why `price` is implausible for `item`, or None if it looks fine."""
    low = item.get("min_price_inr")
    high = item.get("max_price_inr")
    if low is not None and price < low:
        return f"price {price} is below min_price_inr {low}"
    if high is not None and price > high:
        return f"price {price} is above max_price_inr {high}"
    if previous_ok:
        change = (price - previous_ok) / previous_ok
        if abs(change) > MAX_CHANGE_RATIO:
            return (
                f"price {price} differs from previous ok price {previous_ok} by {change * 100:+.1f}% "
                f"(limit {MAX_CHANGE_RATIO * 100:.0f}%)"
            )
    return None


def apply(
    result: price_parser.ParseResult, item: Mapping[str, Any], previous_ok: int | None
) -> price_parser.ParseResult:
    """Downgrade an ok result to suspect when its price is implausible; other results pass through."""
    if result.status != price_parser.STATUS_OK or result.price_inr is None:
        return result
    reason = suspect_reason(result.price_inr, item, previous_ok)
    if reason is None:
        return result
    return dataclasses.replace(result, status=price_parser.STATUS_SUSPECT, error=reason)
