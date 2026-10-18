"""Offline tests for alert rules, weekly summary and Telegram sending (all network mocked)."""

from __future__ import annotations

from datetime import datetime, timedelta

import httpx
import pytest

import alerts
import check_price
import storage

DEV = "dev"
AC = "ac"
URL = "https://www.apple.com/in-edu/shop/buy-mac/x"
ITEMS = [
    {"id": DEV, "name": "Test MacBook", "url": URL, "enabled": True},
    {"id": AC, "name": "Test AppleCare+", "url": URL, "enabled": True},
]
ITEMS_BY_ID = {i["id"]: i for i in ITEMS}
SETTINGS = alerts.AlertSettings(drop_inr=5000, failure_streak=2)
BASE = datetime(2026, 10, 1, 12, 0, tzinfo=storage.IST)
FAKE_TOKEN = "123456:FAKE-token-ABCdef"
FAKE_CHAT = "987654"


def _row(item_id: str, day: float, status: str = "ok", price: int | None = None, error: str = "") -> dict:
    ts = BASE + timedelta(days=day)
    return {
        "timestamp_ist": storage.timestamp_ist(ts),
        "item_id": item_id,
        "line_item": "device",
        "seller": "apple_in_edu",
        "price_type": "education",
        "url": URL,
        "part_number_matched": "Z1KM" if status == "ok" else "",
        "mrp_inr": "",
        "price_inr": str(price) if price is not None else "",
        "in_stock": "",
        "offers_raw": "",
        "status": status,
        "error": error,
    }


def _history(item_id: str, entries: list) -> list[dict]:
    """entries: ints are ok prices, strings are non-ok statuses; one row per day."""
    rows = []
    for day, entry in enumerate(entries):
        if isinstance(entry, int):
            rows.append(_row(item_id, day, price=entry))
        else:
            rows.append(_row(item_id, day, status=entry, error=f"{entry} on day {day}"))
    return rows


def _alerts(entries: list, item_id: str = DEV) -> list[alerts.Alert]:
    return alerts.evaluate_alerts(_history(item_id, entries), [item_id], ITEMS_BY_ID, SETTINGS)


# ------------------------------------------------------------------ price rules


def test_drop_of_exactly_threshold_fires():
    result = _alerts([250000, 260000, 255000])

    assert len(result) == 1
    assert "price drop" in result[0].text
    assert "all-time low" not in result[0].text


def test_drop_one_below_threshold_does_not_fire():
    assert _alerts([250000, 260000, 255001]) == []


def test_first_ever_run_sends_nothing():
    assert _alerts([255000]) == []


def test_first_ok_after_single_failure_sends_nothing():
    assert _alerts(["failed", 255000]) == []


def test_all_time_low_is_strict():
    lower = _alerts([255000, 254000])  # 1000 drop: below drop threshold, but a new low
    equal = _alerts([254000, 256000, 254000])  # equals the earlier low: not a new low

    assert len(lower) == 1 and "new all-time low" in lower[0].text and "price drop" not in lower[0].text
    assert equal == []


def test_drop_and_low_combined_into_one_message():
    result = _alerts([260000, 255000])

    assert len(result) == 1
    assert "price drop + new all-time low" in result[0].text


def test_same_day_rerun_does_not_duplicate_price_alert():
    assert _alerts([260000, 255000, 255000]) == []


def test_failed_rows_are_ignored_for_price_comparison():
    result = _alerts([260000, "blocked", 255000])

    assert len(result) == 1  # compares with previous ok (260000), not the blocked row


def test_price_message_format():
    text = _alerts([260000, 255000])[0].text

    assert "Test MacBook" in text
    assert "₹2,60,000 -> ₹2,55,000" in text
    assert "-₹5,000" in text
    assert "-1.9%" in text
    assert "Lowest ever: ₹2,55,000 on 2026-10-02" in text
    assert URL in text


# ------------------------------------------------------- failure and recovery


@pytest.mark.parametrize("status", ["failed", "blocked", "no_match"])
def test_failure_fires_at_streak_equal_to_threshold(status):
    result = _alerts([255000, "failed", status])

    assert [a.kind for a in result] == ["failure"]
    assert f"{status} on day 2" in result[0].text  # latest error included


def test_failure_does_not_fire_below_or_above_threshold():
    assert _alerts([255000, "failed"]) == []
    assert _alerts([255000, "failed", "failed", "failed"]) == []


def test_failure_error_text_is_trimmed():
    rows = _history(DEV, [255000, "failed"]) + [_row(DEV, 2, "failed", error="x " * 500)]

    text = alerts.evaluate_alerts(rows, [DEV], ITEMS_BY_ID, SETTINGS)[0].text

    error_line = next(line for line in text.splitlines() if line.startswith("Latest error:"))
    assert len(error_line) <= len("Latest error: ") + alerts.ERROR_TEXT_MAX_LEN


def test_recovery_fires_once():
    first = _alerts([255000, "failed", "blocked", 255000])
    second = _alerts([255000, "failed", "blocked", 255000, 255000])

    assert [a.kind for a in first] == ["recovery"]
    assert "working again" in first[0].text
    assert second == []


def test_no_recovery_after_short_failure():
    assert _alerts([255000, "failed", 255000]) == []


def test_no_match_failure_has_distinct_wording():
    text = _alerts([255000, "failed", "no_match"])[0].text

    assert "page or configuration changed (no_match)" in text


def test_failed_status_wording():
    text = _alerts([255000, "no_match", "failed"])[0].text

    assert "Reason: check failed (failed)" in text


def test_reminder_fires_only_at_streak_7():
    def kinds(n_failures: int) -> list[str]:
        return [a.kind for a in _alerts([255000] + ["failed"] * n_failures)]

    assert kinds(2) == ["failure"]
    assert [kinds(n) for n in (3, 4, 5, 6)] == [[], [], [], []]
    assert kinds(7) == ["failure_reminder"]
    assert kinds(8) == [] and kinds(14) == []
    assert "still failing (reminder)" in _alerts([255000] + ["blocked"] * 7)[0].text
    assert "7 consecutive runs not ok" in _alerts([255000] + ["blocked"] * 7)[0].text


def test_recovery_after_reminder_fires_once():
    first = _alerts([255000] + ["failed"] * 9 + [255000])
    second = _alerts([255000] + ["failed"] * 9 + [255000, 255000])

    assert [a.kind for a in first] == ["recovery"]
    assert second == []


def test_suspect_counts_as_failure_and_is_never_a_price():
    rows = _history(DEV, [260000]) + [
        _row(DEV, 1, "suspect", price=1, error="price 1 is below min_price_inr 150000"),
        _row(DEV, 2, "suspect", price=1, error="price 1 is below min_price_inr 150000"),
    ]

    result = alerts.evaluate_alerts(rows, [DEV], ITEMS_BY_ID, SETTINGS)

    assert [a.kind for a in result] == ["failure"]
    assert "implausible price" in result[0].text
    assert "₹1" not in result[0].text.replace("₹1,", "")  # suspect price never shown as a price
    after = rows + [_row(DEV, 3, price=260000)]  # same as last ok price: recovery only, no low
    assert [a.kind for a in alerts.evaluate_alerts(after, [DEV], ITEMS_BY_ID, SETTINGS)] == ["recovery"]


def test_recovery_and_price_drop_are_separate_messages():
    assert [a.kind for a in _alerts([260000, "failed", "failed", 250000])] == ["recovery", "price"]


# --------------------------------------------------------- per-item independence


def test_items_are_evaluated_independently():
    dev = _history(DEV, [260000, 255000])
    ac = _history(AC, [26900, "failed", "failed"])
    rows = [r for pair in zip(dev, ac[: len(dev)], strict=True) for r in pair] + ac[len(dev) :]

    both = alerts.evaluate_alerts(rows, [DEV, AC], ITEMS_BY_ID, SETTINGS)
    only_ac = alerts.evaluate_alerts(rows, [AC], ITEMS_BY_ID, SETTINGS)

    assert [(a.item_id, a.kind) for a in both] == [(DEV, "price"), (AC, "failure")]
    assert [(a.item_id, a.kind) for a in only_ac] == [(AC, "failure")]
    assert "Test AppleCare+" in only_ac[0].text


# ----------------------------------------------------------------- settings


def test_alert_settings_from_config():
    assert alerts.parse_alert_settings({"alerts": {"drop_inr": 3000, "failure_streak": 3}}) == (
        alerts.AlertSettings(3000, 3, 7)
    )
    assert alerts.parse_alert_settings({}) == alerts.AlertSettings(5000, 2, 7)


@pytest.mark.parametrize(
    "bad",
    [
        {"drop_inr": 0},
        {"failure_streak": "2"},
        {"drop_inr": True},
        {"failure_reminder_streak": 2},  # must be greater than failure_streak
        {"failure_reminder_streak": 0},
    ],
)
def test_alert_settings_validation(bad):
    with pytest.raises(ValueError):
        alerts.parse_alert_settings({"alerts": bad})


def test_repo_config_alert_settings():
    settings = check_price.load_alert_settings(check_price.DEFAULT_CONFIG_PATH)

    assert settings == alerts.AlertSettings(5000, 2, 7)


# ------------------------------------------------------------ weekly summary


def _summary_rows() -> list[dict]:
    rows = [
        _row(DEV, 0, price=260000),  # highest ever
        _row(DEV, 2, price=255000),  # baseline: last ok at/before now-7d
        _row(DEV, 4, price=250000),  # lowest ever (first reached on day 4)
        _row(DEV, 5, "blocked", error="HTTP 403"),
        _row(DEV, 6, price=250000),
        _row(DEV, 8, price=252000),  # last ok
        _row(DEV, 9, "failed", error="timeout"),
    ]
    rows += [_row(AC, d, price=26900) for d in (0, 3, 6, 9)]
    return rows


def test_weekly_summary_figures():
    now = BASE + timedelta(days=9, hours=1)  # cutoff = day 2 + 1h

    text = alerts.build_weekly_summary(_summary_rows(), ITEMS, now)

    dev_block, ac_block = text.split("\n\n")[1:]
    assert "Current: ₹2,52,000" in dev_block
    assert "Change vs 7 days ago: -₹3,000 (-1.2%)" in dev_block
    assert "Lowest ever: ₹2,50,000 on 2026-10-05" in dev_block
    assert "Highest ever: ₹2,60,000 on 2026-10-01" in dev_block
    assert "Runs in last 7 days: 3 ok, 2 failed" in dev_block
    assert "Last ok run: 2026-10-09" in dev_block
    assert "No price change this week" not in dev_block

    assert "Change vs 7 days ago: no change" in ac_block
    assert "No price change this week" in ac_block


def test_weekly_summary_with_short_history_and_no_data():
    rows = [_row(DEV, 0, price=255000), _row(DEV, 1, price=255000)]

    text = alerts.build_weekly_summary(rows, ITEMS, BASE + timedelta(days=1))

    assert "Change since first record (2026-10-01): no change" in text
    assert "No successful price check yet (0 run(s) recorded)" in text


def test_weekly_summary_does_not_modify_csv(tmp_path):
    path = tmp_path / "prices.csv"
    storage.append_rows(_summary_rows(), path)
    before = path.read_bytes()

    alerts.run_notifications(
        path, ITEMS, SETTINGS, [], BASE + timedelta(days=9), env={"FORCE_SUMMARY": "1"}, dry_run=True
    )

    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "day,env,expected",
    [
        (datetime(2026, 10, 18, 12, tzinfo=storage.IST), {}, True),  # Sunday
        (datetime(2026, 10, 19, 12, tzinfo=storage.IST), {}, False),  # Monday
        (datetime(2026, 10, 19, 12, tzinfo=storage.IST), {"FORCE_SUMMARY": "1"}, True),
        (datetime(2026, 10, 19, 12, tzinfo=storage.IST), {"FORCE_SUMMARY": "0"}, False),
    ],
)
def test_should_send_summary(day, env, expected):
    assert alerts.should_send_summary(day, env) is expected


# ------------------------------------------------------------------ telegram


def _telegram_client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_send_telegram_posts_message():
    seen: list[httpx.Request] = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    with _telegram_client(handler) as client:
        ok = alerts.send_telegram("Price ₹2,51,900", FAKE_TOKEN, FAKE_CHAT, client=client)

    assert ok is True
    assert len(seen) == 1
    assert seen[0].url.path == f"/bot{FAKE_TOKEN}/sendMessage"
    body = seen[0].read().decode("utf-8")
    assert FAKE_CHAT in body
    assert ("₹" in body) or ("\\u20b9" in body)  # JSON may or may not escape non-ASCII


def test_send_telegram_retries_once():
    responses = iter([httpx.Response(500, json={"ok": False}), httpx.Response(200, json={"ok": True})])
    sleeps: list[float] = []

    with _telegram_client(lambda r: next(responses)) as client:
        ok = alerts.send_telegram("hi", FAKE_TOKEN, FAKE_CHAT, client=client, sleep=sleeps.append)

    assert ok is True
    assert len(sleeps) == 1


def test_send_telegram_failure_never_raises_or_leaks_token(capsys):
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ConnectError(f"cannot reach {request.url}", request=request)

    with _telegram_client(handler) as client:
        ok = alerts.send_telegram("hi", FAKE_TOKEN, FAKE_CHAT, client=client, sleep=lambda s: None)

    out = capsys.readouterr()
    assert ok is False
    assert len(calls) == alerts.TELEGRAM_ATTEMPTS
    assert "Telegram send failed" in out.out
    assert FAKE_TOKEN not in out.out + out.err
    assert "***" in out.out


def test_dry_run_prints_and_never_sends(tmp_path, capsys):
    path = tmp_path / "prices.csv"
    storage.append_rows(_history(DEV, [260000, 255000]), path)
    sent = []

    alerts.run_notifications(
        path, ITEMS, SETTINGS, [DEV], BASE, env={"TELEGRAM_BOT_TOKEN": FAKE_TOKEN,
        "TELEGRAM_CHAT_ID": FAKE_CHAT}, dry_run=True, sender=lambda *a: sent.append(a) or True,
    )

    out = capsys.readouterr().out
    assert sent == []
    assert "[dry run]" in out and "INR 2,55,000" in out
    out.encode("ascii")


def test_missing_credentials_skip_with_one_warning(tmp_path, capsys):
    path = tmp_path / "prices.csv"
    storage.append_rows(_history(DEV, [260000, 255000]), path)
    sent = []

    alerts.run_notifications(
        path, ITEMS, SETTINGS, [DEV], BASE, env={"FORCE_SUMMARY": "1"},
        sender=lambda *a: sent.append(a) or True,
    )

    out = capsys.readouterr().out
    assert sent == []
    assert out.count("WARNING") == 1


def test_credentials_present_sends_each_message(tmp_path):
    path = tmp_path / "prices.csv"
    storage.append_rows(_history(DEV, [260000, 255000]), path)
    sent = []

    alerts.run_notifications(
        path, ITEMS, SETTINGS, [DEV], BASE,
        env={"TELEGRAM_BOT_TOKEN": FAKE_TOKEN, "TELEGRAM_CHAT_ID": FAKE_CHAT, "FORCE_SUMMARY": "1"},
        sender=lambda text, token, chat: sent.append((token, chat)) or True,
    )

    assert sent == [(FAKE_TOKEN, FAKE_CHAT)] * 2  # price alert + weekly summary


def test_format_inr():
    assert alerts.format_inr(999) == "₹999"
    assert alerts.format_inr(5000) == "₹5,000"
    assert alerts.format_inr(251900) == "₹2,51,900"
    assert alerts.format_inr(10000000) == "₹1,00,00,000"
    assert alerts.format_inr(-3000) == "-₹3,000"


# --------------------------------------------- end-to-end through check_price.main


def _patch_network(monkeypatch, live_raw_html, telegram_handler):
    real_client = httpx.Client

    def handler(request):
        if request.url.host == "api.telegram.org":
            return telegram_handler(request)
        return httpx.Response(200, text=live_raw_html)

    def fake_client(**kwargs):
        kwargs.pop("transport", None)
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, "Client", fake_client)
    monkeypatch.setattr(alerts.time, "sleep", lambda s: None)


def test_main_telegram_failure_keeps_data_and_hides_token(monkeypatch, tmp_path, capsys, live_raw_html):
    def telegram_down(request):
        raise httpx.ConnectError(f"cannot reach {request.url}", request=request)

    _patch_network(monkeypatch, live_raw_html, telegram_down)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", FAKE_TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", FAKE_CHAT)
    monkeypatch.setenv("FORCE_SUMMARY", "1")
    csv_path = tmp_path / "prices.csv"

    code = check_price.main(["--csv", str(csv_path)])

    out = capsys.readouterr()
    assert code == check_price.EXIT_SEND_FAILED == 4
    assert len(storage.read_rows(csv_path)) == 2  # rows written before the non-zero exit
    assert "Telegram send failed" in out.out
    assert "could not be delivered" in out.out
    assert FAKE_TOKEN not in out.out + out.err


def test_main_missing_credentials_is_warning_with_exit_0(monkeypatch, tmp_path, capsys, live_raw_html):
    _patch_network(monkeypatch, live_raw_html, lambda r: httpx.Response(200, json={"ok": True}))
    monkeypatch.setenv("FORCE_SUMMARY", "1")  # a message is due, but no credentials are set

    code = check_price.main(["--csv", str(tmp_path / "prices.csv")])

    assert code == 0
    assert "WARNING" in capsys.readouterr().out


def test_main_successful_send_exits_0(monkeypatch, tmp_path, live_raw_html):
    _patch_network(monkeypatch, live_raw_html, lambda r: httpx.Response(200, json={"ok": True}))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", FAKE_TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", FAKE_CHAT)
    monkeypatch.setenv("FORCE_SUMMARY", "1")

    assert check_price.main(["--csv", str(tmp_path / "prices.csv")]) == 0


def test_notify_result_counts_partial_failures(tmp_path):
    path = tmp_path / "prices.csv"
    storage.append_rows(_history(DEV, [260000, 255000]), path)
    replies = iter([True, False])  # price alert sent, weekly summary fails

    result = alerts.run_notifications(
        path, ITEMS, SETTINGS, [DEV], BASE,
        env={"TELEGRAM_BOT_TOKEN": FAKE_TOKEN, "TELEGRAM_CHAT_ID": FAKE_CHAT, "FORCE_SUMMARY": "1"},
        sender=lambda *a: next(replies),
    )

    assert (len(result.messages), result.sent, result.failed) == (2, 1, 1)


def test_main_alert_crash_returns_3_and_keeps_rows(monkeypatch, tmp_path, capsys, live_raw_html):
    _patch_network(monkeypatch, live_raw_html, lambda r: httpx.Response(200, json={"ok": True}))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", FAKE_TOKEN)

    def boom(*args, **kwargs):
        raise RuntimeError(f"bad thing with {FAKE_TOKEN}")

    monkeypatch.setattr(alerts, "run_notifications", boom)
    csv_path = tmp_path / "prices.csv"

    code = check_price.main(["--csv", str(csv_path)])

    out = capsys.readouterr().out
    assert code == check_price.EXIT_NOTIFY_ERROR
    assert len(storage.read_rows(csv_path)) == 2
    assert FAKE_TOKEN not in out


def test_main_dry_run_and_no_alerts_flags(monkeypatch, tmp_path, capsys, live_raw_html):
    sent = []

    def telegram(request):
        sent.append(request)
        return httpx.Response(200, json={"ok": True})

    _patch_network(monkeypatch, live_raw_html, telegram)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", FAKE_TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", FAKE_CHAT)
    monkeypatch.setenv("FORCE_SUMMARY", "1")
    csv_path = tmp_path / "prices.csv"

    assert check_price.main(["--csv", str(csv_path), "--alerts-dry-run"]) == 0
    dry_out = capsys.readouterr().out
    assert check_price.main(["--csv", str(csv_path), "--no-alerts"]) == 0
    no_alerts_out = capsys.readouterr().out

    assert sent == []
    assert "[dry run]" in dry_out and "MacTrack weekly summary" in dry_out
    assert "Alerts disabled" in no_alerts_out and "[dry run]" not in no_alerts_out
