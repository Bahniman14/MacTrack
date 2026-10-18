"""Fetch each enabled item's page once, parse it, and append one CSV row per item.

Usage:  python check_price.py [--config config.json] [--csv docs/data/prices.csv]
                              [--no-alerts | --alerts-dry-run]

Exit codes: 0 = run completed (even if individual items failed),
            3 = rows were appended but alert/summary processing crashed.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

import alerts
import parser as price_parser
import storage

ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = ROOT / "config.json"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)
REQUEST_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-IN,en;q=0.9",
}
REQUEST_TIMEOUT_S = 30.0
MIN_DELAY_S = 3.0
MAX_DELAY_S = 8.0
BLOCKED_HTTP_STATUSES = {403, 429}

REQUIRED_ITEM_KEYS = ("id", "line_item", "seller", "price_type", "url", "part_number", "enabled")
LINE_ITEMS = ("device", "applecare")
EXIT_NOTIFY_ERROR = 3


@dataclass(frozen=True)
class FetchResult:
    html: str | None
    status: str | None = None  # set only when the fetch itself failed / was blocked
    error: str | None = None


def _read_config(path: Path) -> dict[str, Any]:
    with Path(path).open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a JSON object")
    return data


def load_alert_settings(path: Path) -> alerts.AlertSettings:
    return alerts.parse_alert_settings(_read_config(path))


def load_config(path: Path) -> list[dict[str, Any]]:
    data = _read_config(path)
    items = data.get("items")
    if not isinstance(items, list):
        raise ValueError(f"{path}: expected an object with an 'items' list")

    ids = set()
    for item in items:
        missing = [k for k in REQUIRED_ITEM_KEYS if k not in item]
        if missing:
            raise ValueError(f"{path}: item {item.get('id', '?')} missing keys {missing}")
        if item["line_item"] not in LINE_ITEMS:
            raise ValueError(f"{path}: item {item['id']} has invalid line_item {item['line_item']!r}")
        if item["id"] in ids:
            raise ValueError(f"{path}: duplicate item id {item['id']!r}")
        ids.add(item["id"])
    return items


def fetch(client: httpx.Client, url: str) -> FetchResult:
    try:
        response = client.get(url)
    except httpx.HTTPError as exc:
        return FetchResult(None, price_parser.STATUS_FAILED, f"{type(exc).__name__}: {exc}")
    if response.status_code in BLOCKED_HTTP_STATUSES:
        return FetchResult(None, price_parser.STATUS_BLOCKED, f"HTTP {response.status_code}")
    if response.status_code != 200:
        return FetchResult(None, price_parser.STATUS_FAILED, f"HTTP {response.status_code}")
    return FetchResult(response.text)


def check_item(item: dict[str, Any], fetched: FetchResult) -> dict[str, Any]:
    """Build one CSV row for an item. Never raises for item-level problems."""
    if fetched.html is None:
        result = price_parser.ParseResult(status=fetched.status, error=fetched.error)
    else:
        try:
            result = price_parser.parse(fetched.html, item)
        except Exception as exc:  # noqa: BLE001 - one bad item must not stop the run
            result = price_parser.ParseResult(
                status=price_parser.STATUS_FAILED, error=f"{type(exc).__name__}: {exc}"
            )
    return {
        "timestamp_ist": storage.timestamp_ist(),
        "item_id": item["id"],
        "line_item": item["line_item"],
        "seller": item["seller"],
        "price_type": item["price_type"],
        "url": item["url"],
        "part_number_matched": result.part_number_matched,
        "mrp_inr": result.mrp_inr,
        "price_inr": result.price_inr,
        "in_stock": result.in_stock,
        "offers_raw": result.offers_raw,
        "status": result.status,
        "error": result.error,
    }


def run(
    items: list[dict[str, Any]],
    client: httpx.Client,
    sleep: Callable[[float], None] = time.sleep,
) -> list[dict[str, Any]]:
    """Fetch each unique URL once (with a polite delay between URLs) and parse every item on it."""
    enabled = [i for i in items if i.get("enabled")]
    urls = list(dict.fromkeys(i["url"] for i in enabled))
    rows: list[dict[str, Any]] = []
    for index, url in enumerate(urls):
        if index > 0:
            sleep(random.uniform(MIN_DELAY_S, MAX_DELAY_S))
        fetched = fetch(client, url)
        rows.extend(check_item(item, fetched) for item in enabled if item["url"] == url)
    return rows


def format_summary(row: dict[str, Any]) -> str:
    def inr(v: Any) -> str:
        return f"INR {v}" if v is not None else "-"

    in_stock = {True: "yes", False: "no", None: "unknown"}[row["in_stock"]]
    line = (
        f"[{row['status']}] {row['item_id']}: price={inr(row['price_inr'])} "
        f"mrp={inr(row['mrp_inr'])} part={row['part_number_matched'] or '-'} in_stock={in_stock}"
    )
    if row["error"]:
        line += f" error={row['error']}"
    # Console may be cp1252 on Windows: never print characters it can't encode.
    return line.encode("ascii", "replace").decode("ascii")


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    items = load_config(args.config)
    settings = load_alert_settings(args.config)
    if not any(i.get("enabled") for i in items):
        print("No enabled items in config.")
        return 0

    with httpx.Client(
        headers=REQUEST_HEADERS, timeout=REQUEST_TIMEOUT_S, follow_redirects=True
    ) as client:
        rows = run(items, client)

    written = storage.append_rows(rows, args.csv)
    for row in rows:
        print(format_summary(row))
    print(f"Appended {written} row(s) to {args.csv}")

    if args.no_alerts:
        print("Alerts disabled (--no-alerts).")
        return 0
    return notify(args, items, settings, [r["item_id"] for r in rows])


def notify(
    args: argparse.Namespace,
    items: list[dict[str, Any]],
    settings: alerts.AlertSettings,
    item_ids: list[str],
) -> int:
    """Alerts + weekly summary. Runs after the CSV append, so data is never lost if this fails."""
    try:
        alerts.run_notifications(
            args.csv,
            items,
            settings,
            item_ids,
            now=storage.now_ist(),
            env=os.environ,
            dry_run=args.alerts_dry_run,
        )
    except Exception as exc:  # noqa: BLE001 - report, keep appended data, signal via exit code
        message = alerts.redact(f"{type(exc).__name__}: {exc}", os.environ.get(alerts.TOKEN_ENV))
        print(f"ERROR: alert/summary processing failed: {alerts.console_safe(message)}")
        return EXIT_NOTIFY_ERROR
    return 0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Check configured prices and append to CSV.")
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    ap.add_argument("--csv", type=Path, default=storage.DEFAULT_CSV_PATH)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--no-alerts", action="store_true", help="skip alerts and weekly summary")
    mode.add_argument(
        "--alerts-dry-run", action="store_true", help="print alert/summary messages instead of sending"
    )
    return ap.parse_args(argv)


if __name__ == "__main__":
    sys.exit(main())
