"""Offline tests for the runner: network is replaced by httpx.MockTransport."""

from __future__ import annotations

import csv
import json

import httpx
import pytest

import check_price


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_same_url_fetched_once_for_device_and_applecare(live_raw_html, config_items):
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, text=live_raw_html)

    sleeps: list[float] = []
    with _client(handler) as client:
        rows = check_price.run(list(config_items.values()), client, sleep=sleeps.append)

    assert len(calls) == 1  # disabled Space Black entry is skipped, shared URL reused
    assert sleeps == []
    assert [(r["item_id"], r["status"], r["price_inr"]) for r in rows] == [
        ("m5_mbp14_silver_24_1tb", "ok", 251900),
        ("m5_mbp14_silver_24_1tb_applecare", "ok", 26900),
    ]


def test_delay_between_different_urls(fixture_html, device_item):
    other = {**device_item, "id": "other", "url": "https://www.apple.com/in-edu/other"}
    sleeps: list[float] = []

    with _client(lambda r: httpx.Response(200, text=fixture_html)) as client:
        check_price.run([device_item, other], client, sleep=sleeps.append)

    assert len(sleeps) == 1
    assert check_price.MIN_DELAY_S <= sleeps[0] <= check_price.MAX_DELAY_S


@pytest.mark.parametrize("code,status", [(403, "blocked"), (429, "blocked"), (500, "failed")])
def test_http_errors_recorded_per_item(device_item, applecare_item, code, status):
    with _client(lambda r: httpx.Response(code, text="nope")) as client:
        rows = check_price.run([device_item, applecare_item], client, sleep=lambda s: None)

    assert [r["status"] for r in rows] == [status, status]
    assert all(r["error"] == f"HTTP {code}" for r in rows)


def test_network_error_does_not_stop_other_urls(fixture_html, device_item):
    broken = {**device_item, "id": "broken", "url": "https://broken.invalid/"}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "broken.invalid":
            raise httpx.ConnectError("boom", request=request)
        return httpx.Response(200, text=fixture_html)

    with _client(handler) as client:
        rows = check_price.run([broken, device_item], client, sleep=lambda s: None)

    assert [r["status"] for r in rows] == ["failed", "ok"]


def test_parser_crash_becomes_failed_row(monkeypatch, fixture_html, device_item):
    def explode(html, item):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(check_price.price_parser, "parse", explode)
    fetched = check_price.FetchResult(fixture_html)

    row = check_price.check_item(device_item, fetched)

    assert row["status"] == "failed"
    assert "RuntimeError" in row["error"]


def test_main_appends_rows_and_prints_ascii_summary(monkeypatch, tmp_path, capsys, live_raw_html):
    csv_path = tmp_path / "prices.csv"
    real_client = httpx.Client

    def fake_client(**kwargs):
        kwargs.pop("transport", None)
        return real_client(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, text=live_raw_html)), **kwargs
        )

    monkeypatch.setattr(check_price.httpx, "Client", fake_client)

    assert check_price.main(["--csv", str(csv_path)]) == 0
    first = csv_path.read_bytes()
    assert check_price.main(["--csv", str(csv_path)]) == 0

    assert csv_path.read_bytes().startswith(first)
    with csv_path.open(encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 4
    out = capsys.readouterr().out
    assert [r["status"] for r in rows] == ["ok"] * 4
    assert "INR 251900" in out and "INR 26900" in out
    out.encode("ascii")  # nothing a cp1252 console could choke on


def test_load_config_validates(tmp_path):
    bad = tmp_path / "config.json"
    bad.write_text(json.dumps({"items": [{"id": "x"}]}), encoding="utf-8")

    with pytest.raises(ValueError, match="missing keys"):
        check_price.load_config(bad)


def test_load_config_rejects_duplicate_ids(tmp_path, device_item):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"items": [device_item, device_item]}), encoding="utf-8")

    with pytest.raises(ValueError, match="duplicate"):
        check_price.load_config(path)


def test_repo_config_is_valid():
    items = check_price.load_config(check_price.DEFAULT_CONFIG_PATH)

    assert [i["id"] for i in items if i["enabled"]] == [
        "m5_mbp14_silver_24_1tb",
        "m5_mbp14_silver_24_1tb_applecare",
    ]
