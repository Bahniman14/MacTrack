"""Price plausibility: config bounds and the 25% jump rule -> status "suspect"."""

from __future__ import annotations

import json

import httpx
import pytest

import check_price
import sanity
import storage
from parser import STATUS_FAILED, STATUS_OK, STATUS_SUSPECT, ParseResult

DEVICE = {"id": "dev", "min_price_inr": 150000, "max_price_inr": 400000}


def _ok(price: int) -> ParseResult:
    return ParseResult(status=STATUS_OK, price_inr=price, part_number_matched="Z1KM")


@pytest.mark.parametrize("price", [1, 149999, 400001])
def test_out_of_bounds_is_suspect(price):
    result = sanity.apply(_ok(price), DEVICE, previous_ok=None)

    assert result.status == STATUS_SUSPECT
    assert str(price) in result.error
    assert result.price_inr == price  # kept in the row for transparency


@pytest.mark.parametrize("price", [150000, 251900, 400000])
def test_in_bounds_first_run_is_ok(price):
    assert sanity.apply(_ok(price), DEVICE, previous_ok=None).status == STATUS_OK


def test_jump_over_25_percent_is_suspect_and_names_both_prices():
    result = sanity.apply(_ok(180000), DEVICE, previous_ok=251900)  # -28.5%

    assert result.status == STATUS_SUSPECT
    assert "180000" in result.error and "251900" in result.error


def test_changes_up_to_25_percent_are_ok():
    assert sanity.apply(_ok(200000), DEVICE, previous_ok=251900).status == STATUS_OK  # -20.6%
    assert sanity.apply(_ok(250000), DEVICE, previous_ok=200000).status == STATUS_OK  # exactly +25%
    assert sanity.apply(_ok(250001), DEVICE, previous_ok=200000).status == STATUS_SUSPECT


def test_items_without_bounds_only_use_the_jump_rule():
    assert sanity.apply(_ok(1), {"id": "x"}, previous_ok=None).status == STATUS_OK
    assert sanity.apply(_ok(1), {"id": "x"}, previous_ok=100).status == STATUS_SUSPECT


def test_non_ok_results_pass_through():
    failed = ParseResult(status=STATUS_FAILED, error="boom")

    assert sanity.apply(failed, DEVICE, previous_ok=251900) is failed


def test_previous_ok_prices_uses_latest_ok_row_per_item():
    rows = [
        {"item_id": "dev", "status": "ok", "price_inr": "260000"},
        {"item_id": "ac", "status": "ok", "price_inr": "26900"},
        {"item_id": "dev", "status": "ok", "price_inr": "251900"},
        {"item_id": "dev", "status": "suspect", "price_inr": "1"},
        {"item_id": "dev", "status": "failed", "price_inr": ""},
    ]

    assert sanity.previous_ok_prices(rows) == {"dev": 251900, "ac": 26900}


def _ld_page(price, sku: str) -> str:
    offer = {"priceCurrency": "INR", "price": price, "sku": sku}
    payload = json.dumps({"@type": "Product", "offers": [offer]})
    return f'<script type="application/ld+json">{payload}</script>'


def test_runner_records_price_1_as_suspect(live_device_item):
    page = _ld_page(1, live_device_item["expected_sku"])

    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=page))) as client:
        rows = check_price.run([live_device_item], client, sleep=lambda s: None)

    assert rows[0]["status"] == STATUS_SUSPECT
    assert "min_price_inr" in rows[0]["error"]


def test_runner_uses_previous_ok_price_for_jump_rule(live_device_item):
    page = _ld_page(180000, live_device_item["expected_sku"])
    previous = {live_device_item["id"]: 251900}

    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=page))) as client:
        rows = check_price.run([live_device_item], client, sleep=lambda s: None, previous_ok_prices=previous)

    assert rows[0]["status"] == STATUS_SUSPECT
    assert "251900" in rows[0]["error"]


def test_main_reads_previous_price_from_csv(monkeypatch, tmp_path, live_raw_html):
    csv_path = tmp_path / "prices.csv"
    csv_path.write_bytes(
        b"timestamp_ist,item_id,line_item,seller,price_type,url,part_number_matched,mrp_inr,price_inr,"
        b"in_stock,offers_raw,status,error\r\n"
        b"2026-10-01T12:00:00+05:30,m5_mbp14_silver_24_1tb,device,apple_in_edu,education,u,Z1KM,,"
        b"400000,,,ok,\r\n"
    )
    real_client = httpx.Client

    def fake_client(**kwargs):
        return real_client(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, text=live_raw_html)),
            **{k: v for k, v in kwargs.items() if k != "transport"},
        )

    monkeypatch.setattr(check_price.httpx, "Client", fake_client)

    assert check_price.main(["--csv", str(csv_path), "--no-alerts"]) == 0

    new = {r["item_id"]: r for r in storage.read_rows(csv_path)[1:]}
    assert new["m5_mbp14_silver_24_1tb"]["status"] == STATUS_SUSPECT  # 251900 vs 400000 is -37%
    assert new["m5_mbp14_silver_24_1tb_applecare"]["status"] == STATUS_OK


@pytest.mark.parametrize(
    "bounds,message",
    [
        ({"min_price_inr": 0}, "positive integer"),
        ({"max_price_inr": "400000"}, "positive integer"),
        ({"min_price_inr": 5, "max_price_inr": 5}, "below"),
    ],
)
def test_load_config_validates_price_bounds(tmp_path, live_device_item, bounds, message):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"items": [{**live_device_item, **bounds}]}), encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        check_price.load_config(path)


def test_repo_config_has_bounds(config_items):
    device = config_items["m5_mbp14_silver_24_1tb"]
    applecare = config_items["m5_mbp14_silver_24_1tb_applecare"]

    assert (device["min_price_inr"], device["max_price_inr"]) == (150000, 400000)
    assert (applecare["min_price_inr"], applecare["max_price_inr"]) == (10000, 60000)
