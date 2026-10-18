"""Telegram alerts and weekly summary, derived entirely from docs/data/prices.csv.

Secrets (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID) come only from the environment and are never
printed: anything printed that could contain the token goes through redact().
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

import storage

TELEGRAM_API = "https://api.telegram.org"
TELEGRAM_TIMEOUT_S = 20.0
TELEGRAM_ATTEMPTS = 2  # one retry
TELEGRAM_RETRY_DELAY_S = 2.0
TELEGRAM_MAX_LEN = 4096
ERROR_TEXT_MAX_LEN = 200
SUMMARY_WINDOW = timedelta(days=7)
SUNDAY = 6

TOKEN_ENV = "TELEGRAM_BOT_TOKEN"
CHAT_ID_ENV = "TELEGRAM_CHAT_ID"
FORCE_SUMMARY_ENV = "FORCE_SUMMARY"

STATUS_OK = "ok"


FAILURE_REASONS = {
    "no_match": "page or configuration changed (no_match)",
    "blocked": "blocked by Apple (blocked)",
    "suspect": "implausible price, not recorded as a price (suspect)",
    "failed": "check failed (failed)",
}


@dataclass(frozen=True)
class AlertSettings:
    drop_inr: int = 5000
    failure_streak: int = 2
    failure_reminder_streak: int = 7


@dataclass(frozen=True)
class Alert:
    item_id: str
    kind: str  # "price" | "failure" | "failure_reminder" | "recovery"
    text: str


@dataclass(frozen=True)
class NotifyResult:
    messages: list[str]
    sent: int = 0
    failed: int = 0  # due messages that could not be delivered although credentials were set


@dataclass(frozen=True)
class _Point:
    ts: datetime
    date: str
    price: int


# ------------------------------------------------------------------- settings


def parse_alert_settings(config: Mapping[str, Any]) -> AlertSettings:
    raw = config.get("alerts") or {}
    if not isinstance(raw, Mapping):
        raise ValueError("config 'alerts' must be an object")
    defaults = AlertSettings()
    drop = raw.get("drop_inr", defaults.drop_inr)
    streak = raw.get("failure_streak", defaults.failure_streak)
    reminder = raw.get("failure_reminder_streak", defaults.failure_reminder_streak)
    named = (("drop_inr", drop), ("failure_streak", streak), ("failure_reminder_streak", reminder))
    for name, value in named:
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise ValueError(f"config alerts.{name} must be a positive integer, got {value!r}")
    if reminder <= streak:
        raise ValueError("config alerts.failure_reminder_streak must be greater than failure_streak")
    return AlertSettings(drop_inr=drop, failure_streak=streak, failure_reminder_streak=reminder)


# --------------------------------------------------------------- alert rules


def evaluate_alerts(
    rows: Sequence[Mapping[str, str]],
    item_ids: Iterable[str],
    items_by_id: Mapping[str, Mapping[str, Any]],
    settings: AlertSettings,
) -> list[Alert]:
    """Compare each item's newest CSV row against its earlier history."""
    alerts: list[Alert] = []
    for item_id in dict.fromkeys(item_ids):
        history = [r for r in rows if r.get("item_id") == item_id]
        if history:
            alerts.extend(_item_alerts(history, items_by_id.get(item_id, {}), item_id, settings))
    return alerts


def _item_alerts(
    history: list[Mapping[str, str]], item: Mapping[str, Any], item_id: str, settings: AlertSettings
) -> list[Alert]:
    newest = history[-1]
    name = item.get("name") or item_id
    url = item.get("url") or newest.get("url", "")

    if not _is_ok(newest):
        streak = _failure_streak(history)
        if streak == settings.failure_streak:
            return [Alert(item_id, "failure", _failure_text("check failing", newest, streak, name, url))]
        if streak == settings.failure_reminder_streak:
            text = _failure_text("still failing (reminder)", newest, streak, name, url)
            return [Alert(item_id, "failure_reminder", text)]
        return []

    alerts: list[Alert] = []
    if _failure_streak(history[:-1]) >= settings.failure_streak:
        text = f"MacTrack: working again\n{name}\nLatest price: {format_inr(_price(newest))}\n{url}"
        alerts.append(Alert(item_id, "recovery", text))

    price_alert = _price_alert(history, name, url, item_id, settings)
    if price_alert:
        alerts.append(price_alert)
    return alerts


def _price_alert(
    history: list[Mapping[str, str]], name: str, url: str, item_id: str, settings: AlertSettings
) -> Alert | None:
    points = _ok_points(history)
    if len(points) < 2:
        return None  # first ever ok price: nothing to compare against
    new, previous, earlier = points[-1], points[-2], points[:-1]

    is_drop = previous.price - new.price >= settings.drop_inr
    is_low = new.price < min(p.price for p in earlier)
    if not (is_drop or is_low):
        return None

    titles = [t for t, on in (("price drop", is_drop), ("new all-time low", is_low)) if on]
    diff = new.price - previous.price
    lowest = min(points, key=lambda p: (p.price, p.ts))
    text = (
        f"MacTrack: {' + '.join(titles)}\n{name}\n"
        f"{format_inr(previous.price)} -> {format_inr(new.price)} "
        f"({_signed_inr(diff)}, {_pct(diff, previous.price)})\n"
        f"Lowest ever: {format_inr(lowest.price)} on {lowest.date}\n{url}"
    )
    return Alert(item_id, "price", text)


def _failure_text(title: str, newest: Mapping[str, str], streak: int, name: str, url: str) -> str:
    status = newest.get("status") or "unknown"
    reason = FAILURE_REASONS.get(status, f"unexpected status ({status})")
    error = _trim(newest.get("error") or "(no error text)")
    return (
        f"MacTrack: {title}\n{name}\n"
        f"Reason: {reason}\n"
        f"{streak} consecutive runs not ok\n"
        f"Latest error: {error}\n{url}"
    )


def _failure_streak(history: Sequence[Mapping[str, str]]) -> int:
    streak = 0
    for row in reversed(history):
        if _is_ok(row):
            break
        streak += 1
    return streak


# ------------------------------------------------------------- weekly summary


def should_send_summary(now: datetime, env: Mapping[str, str]) -> bool:
    return now.weekday() == SUNDAY or env.get(FORCE_SUMMARY_ENV, "").strip() == "1"


def build_weekly_summary(
    rows: Sequence[Mapping[str, str]], items: Sequence[Mapping[str, Any]], now: datetime
) -> str:
    cutoff = now - SUMMARY_WINDOW
    blocks = [f"MacTrack weekly summary ({now.date().isoformat()})"]
    for item in items:
        history = [r for r in rows if r.get("item_id") == item["id"]]
        blocks.append(_summary_block(history, item, cutoff))
    return "\n\n".join(blocks)


def _summary_block(history: list[Mapping[str, str]], item: Mapping[str, Any], cutoff: datetime) -> str:
    name = item.get("name") or item["id"]
    points = _ok_points(history)
    if not points:
        return f"{name}\nNo successful price check yet ({len(history)} run(s) recorded)."

    current = points[-1]
    lowest = min(points, key=lambda p: (p.price, p.ts))
    highest = max(points, key=lambda p: (p.price, -p.ts.timestamp()))
    recent = [r for r in history if _parse_ts(r) > cutoff]
    ok_recent = sum(1 for r in recent if _is_ok(r))

    before = [p for p in points if p.ts <= cutoff]
    baseline = before[-1] if before else points[0]
    label = "vs 7 days ago" if before else f"since first record ({baseline.date})"
    diff = current.price - baseline.price
    change = "no change" if diff == 0 else f"{_signed_inr(diff)} ({_pct(diff, baseline.price)})"

    lines = [
        name,
        f"Current: {format_inr(current.price)}",
        f"Change {label}: {change}",
        f"Lowest ever: {format_inr(lowest.price)} on {lowest.date}",
        f"Highest ever: {format_inr(highest.price)} on {highest.date}",
        f"Runs in last 7 days: {ok_recent} ok, {len(recent) - ok_recent} failed",
        f"Last ok run: {current.date}",
    ]
    week_prices = {baseline.price} | {p.price for p in points if p.ts > cutoff}
    if len(week_prices) == 1:
        lines.append("No price change this week")
    return "\n".join(lines)


# ------------------------------------------------------------------- telegram


def get_credentials(env: Mapping[str, str]) -> tuple[str, str] | None:
    token = (env.get(TOKEN_ENV) or "").strip()
    chat_id = (env.get(CHAT_ID_ENV) or "").strip()
    return (token, chat_id) if token and chat_id else None


def send_telegram(
    text: str,
    token: str,
    chat_id: str,
    client: httpx.Client | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """Send one message. Returns True on success; never raises; never prints the token."""
    payload = {
        "chat_id": chat_id,
        "text": text[:TELEGRAM_MAX_LEN],
        "disable_web_page_preview": True,
    }
    url = f"{TELEGRAM_API}/bot{token}/sendMessage"
    own_client = client is None
    client = client or httpx.Client(timeout=TELEGRAM_TIMEOUT_S)
    last_error = "unknown error"
    try:
        for attempt in range(1, TELEGRAM_ATTEMPTS + 1):
            try:
                response = client.post(url, json=payload, timeout=TELEGRAM_TIMEOUT_S)
                if response.status_code == 200 and _telegram_ok(response):
                    return True
                last_error = f"HTTP {response.status_code}: {_telegram_description(response)}"
            except httpx.HTTPError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            except Exception as exc:  # noqa: BLE001 - alerts must never crash the run
                last_error = f"{type(exc).__name__}: {exc}"
            if attempt < TELEGRAM_ATTEMPTS:
                sleep(TELEGRAM_RETRY_DELAY_S)
    finally:
        if own_client:
            client.close()
    print(f"WARNING: Telegram send failed: {redact(last_error, token)}")
    return False


def _telegram_ok(response: httpx.Response) -> bool:
    try:
        return bool(response.json().get("ok"))
    except ValueError:
        return False


def _telegram_description(response: httpx.Response) -> str:
    try:
        return str(response.json().get("description", ""))[:ERROR_TEXT_MAX_LEN]
    except ValueError:
        return ""


def redact(text: str, *secrets: str | None) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return text


def console_safe(text: str) -> str:
    """cp1252-safe console text: rupee symbol -> INR, other non-ASCII -> '?'."""
    return text.replace("₹", "INR ").encode("ascii", "replace").decode("ascii")


# --------------------------------------------------------------- orchestration


def run_notifications(
    csv_path: Path,
    items: Sequence[Mapping[str, Any]],
    settings: AlertSettings,
    item_ids: Iterable[str],
    now: datetime,
    env: Mapping[str, str] | None = None,
    dry_run: bool = False,
    sender: Callable[[str, str, str], bool] = send_telegram,
) -> NotifyResult:
    """Evaluate alerts (after rows are appended) and the weekly summary; send or print them.

    Reads the CSV only. Returns the due messages and how many were sent / failed to send.
    """
    env = os.environ if env is None else env
    rows = storage.read_rows(csv_path)
    items_by_id = {i["id"]: i for i in items}
    messages = [a.text for a in evaluate_alerts(rows, item_ids, items_by_id, settings)]
    if should_send_summary(now, env):
        enabled = [i for i in items if i.get("enabled")]
        messages.append(build_weekly_summary(rows, enabled, now))

    if not messages:
        print("Alerts: nothing to send.")
        return NotifyResult(messages)
    if dry_run:
        for text in messages:
            print(f"--- [dry run] would send to Telegram ---\n{console_safe(text)}")
        return NotifyResult(messages)

    creds = get_credentials(env)
    if creds is None:
        print(
            f"WARNING: {TOKEN_ENV}/{CHAT_ID_ENV} not set; skipping {len(messages)} Telegram message(s)."
        )
        return NotifyResult(messages)
    sent = sum(1 for text in messages if sender(text, *creds))
    print(f"Alerts: sent {sent}/{len(messages)} Telegram message(s).")
    return NotifyResult(messages, sent=sent, failed=len(messages) - sent)


# -------------------------------------------------------------------- helpers


def _is_ok(row: Mapping[str, str]) -> bool:
    return row.get("status") == STATUS_OK and _price(row) is not None


def _price(row: Mapping[str, str]) -> int | None:
    raw = (row.get("price_inr") or "").strip()
    return int(raw) if raw.isdigit() else None


def _parse_ts(row: Mapping[str, str]) -> datetime:
    return datetime.fromisoformat(row["timestamp_ist"]).astimezone(storage.IST)


def _ok_points(history: Iterable[Mapping[str, str]]) -> list[_Point]:
    points = []
    for row in history:
        if _is_ok(row):
            ts = _parse_ts(row)
            points.append(_Point(ts=ts, date=ts.date().isoformat(), price=_price(row)))
    return points


def format_inr(amount: int) -> str:
    """Indian digit grouping with rupee symbol, e.g. 251900 -> ₹2,51,900."""
    sign = "-" if amount < 0 else ""
    digits = str(abs(amount))
    if len(digits) > 3:
        head, tail = digits[:-3], digits[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        digits = ",".join([*groups, tail])
    return f"{sign}₹{digits}"


def _signed_inr(diff: int) -> str:
    return f"+{format_inr(diff)}" if diff > 0 else format_inr(diff)


def _pct(diff: int, base: int) -> str:
    return f"{diff / base * 100:+.1f}%" if base else "n/a"


def _trim(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= ERROR_TEXT_MAX_LEN else text[: ERROR_TEXT_MAX_LEN - 3] + "..."
