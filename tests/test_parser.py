from __future__ import annotations

import json

import pytest

from parser import (
    STATUS_BLOCKED,
    STATUS_FAILED,
    STATUS_NO_MATCH,
    STATUS_OK,
    parse,
)
from tests.conftest import SKU_70W, SKU_96W

COMBINED_PRICE = 281900  # device + AppleCare+ headline price: must never be recorded
COMBINED_MRP = 296900
LIVE_DEVICE_PRICE = 251900  # Product JSON-LD price in tests/fixtures/apple_live_raw.html


# ---------------------------------------------------- live raw response (httpx bytes)


def test_live_raw_device_ok_without_mrp_or_add_to_bag(live_raw_html, live_device_item):
    result = parse(live_raw_html, live_device_item)

    assert result.status == STATUS_OK, result.error
    assert result.price_inr == LIVE_DEVICE_PRICE
    assert result.part_number_matched == "Z1KM"
    assert result.mrp_inr is None  # device MRP block is not in the raw HTML
    assert result.in_stock is None  # Add to Bag form is not in the raw HTML


def test_live_raw_applecare_ok(live_raw_html, applecare_item):
    result = parse(live_raw_html, applecare_item)

    assert result.status == STATUS_OK, result.error
    assert result.price_inr == 26900
    assert result.mrp_inr == 29900
    assert result.part_number_matched == "SXKK2HN/A"
    assert result.in_stock is None


def test_live_raw_contains_configured_sku(live_raw_html, live_device_item):
    assert live_device_item["expected_sku"] == SKU_70W
    assert f'"sku":"{SKU_70W}"' in live_raw_html


# ---------------------------------------------------------------- device


def test_device_price_mrp_and_part_from_fixture(fixture_html, device_item):
    result = parse(fixture_html, device_item)

    assert result.status == STATUS_OK, result.error
    assert result.price_inr == 255000
    assert result.mrp_inr == 267000
    assert result.part_number_matched == "Z1KM"
    assert result.error is None


def test_device_in_stock_true_when_add_to_bag_form_present(fixture_html, device_item):
    assert parse(fixture_html, device_item).in_stock is True


def test_device_offers_raw_contains_creator_studio_trial_text(fixture_html, device_item):
    offers = parse(fixture_html, device_item).offers_raw

    assert offers is not None
    assert "free three-month trial of Apple Creator Studio" in offers


@pytest.mark.parametrize(
    "html_fixture,expected_sku,page_sku",
    [("fixture_html", SKU_70W, SKU_96W), ("live_raw_html", SKU_96W, SKU_70W)],
)
def test_device_same_prefix_different_sku_is_no_match(
    request, live_device_item, html_fixture, expected_sku, page_sku
):
    html = request.getfixturevalue(html_fixture)

    result = parse(html, {**live_device_item, "expected_sku": expected_sku})

    assert result.status == STATUS_NO_MATCH
    assert result.price_inr is None
    assert expected_sku in result.error
    assert page_sku in result.error


@pytest.mark.parametrize("part_number", ["MGDN4HN/A", "Z1KN", "TODO_PART_NUMBER"])
def test_device_wrong_part_number_is_no_match(fixture_html, device_item, part_number):
    result = parse(fixture_html, {**device_item, "part_number": part_number})

    assert result.status == STATUS_NO_MATCH
    assert result.price_inr is None


def test_device_without_json_ld_fails(device_item):
    html = '<html><body><form data-part-number="Z1KM"></form></body></html>'

    result = parse(html, device_item)

    assert result.status == STATUS_FAILED
    assert "JSON-LD" in result.error


def test_device_in_stock_unknown_without_form(device_item):
    sku = device_item["expected_sku"]
    html = (
        '<script type="application/ld+json">{"@type":"Product","offers":[{"@type":"Offer",'
        f'"priceCurrency":"INR","price":251900.00,"sku":"{sku}"}}]}}</script>'
    )

    result = parse(html, device_item)

    assert result.status == STATUS_OK
    assert result.price_inr == 251900
    assert result.mrp_inr is None  # not present -> not guessed
    assert result.in_stock is None  # not present -> not guessed


# ------------------------------------------------------------- applecare


def test_applecare_price_mrp_and_part_from_fixture(fixture_html, applecare_item):
    result = parse(fixture_html, applecare_item)

    assert result.status == STATUS_OK, result.error
    assert result.price_inr == 26900
    assert result.mrp_inr == 29900
    assert result.part_number_matched == "SXKK2HN/A"
    assert result.in_stock is True


@pytest.mark.parametrize("part_number", ["SCXV3HN/A", "SCXW3HN/A", "MGDN4HN/A"])
def test_applecare_other_plan_than_page_acpart_is_no_match(fixture_html, applecare_item, part_number):
    result = parse(fixture_html, {**applecare_item, "part_number": part_number})

    assert result.status == STATUS_NO_MATCH
    assert result.price_inr is None


def test_applecare_uses_bootstrap_and_price_map_when_form_absent(applecare_item):
    html = (
        "<script>window.APPLECARE_BOOTSTRAP = {\n appleCareData: [{\"appleCarePlans\":[{"
        '"moreOptions":{"appleCaresPriceMap":{"SXKK2HN/A":{"fullMrpPrice":"MRP ₹29900.00"}}},'
        '"planDetails":[{"partNumber":"SXKK2HN/A","rawPrice":26900.00,'
        '"dimensionPaymentType":{"value":"fullprice"}}]}]}]\n};</script>'
    )

    result = parse(html, applecare_item)

    assert result.status == STATUS_OK, result.error
    assert result.price_inr == 26900
    assert result.mrp_inr == 29900
    assert result.in_stock is None


def _bootstrap(plans: list[dict]) -> str:
    return "<script>window.APPLECARE_BOOTSTRAP = {\n appleCareData: " + json.dumps(
        [{"planDetails": plans}]
    ) + "\n};</script>"


FULL = {"dimensionPaymentType": {"value": "fullprice"}}


def test_applecare_monthly_plan_listed_first_is_ignored(applecare_item):
    html = _bootstrap(
        [
            {"partNumber": "SXKK2HN/A", "rawPrice": 1317.0},  # monthly variant, no payment type
            {"partNumber": "SXKK2HN/A", "rawPrice": 1317.0, "dimensionPaymentType": {"value": "financing"}},
            {"partNumber": "SXKK2HN/A", "rawPrice": 26900.0, **FULL},
        ]
    )

    result = parse(html, applecare_item)

    assert result.status == STATUS_OK, result.error
    assert result.price_inr == 26900


def test_applecare_without_explicit_fullprice_is_no_match(applecare_item):
    result = parse(_bootstrap([{"partNumber": "SXKK2HN/A", "rawPrice": 26900.0}]), applecare_item)

    assert result.status == STATUS_NO_MATCH
    assert "full-price" in result.error


def test_applecare_duplicate_plans_with_different_prices_are_ambiguous(applecare_item):
    html = _bootstrap(
        [
            {"partNumber": "SXKK2HN/A", "rawPrice": 26900.0, **FULL},
            {"partNumber": "SXKK2HN/A", "rawPrice": 29900.0, **FULL},
        ]
    )

    result = parse(html, applecare_item)

    assert result.status == STATUS_FAILED
    assert result.price_inr is None
    assert "ambiguous" in result.error


def test_applecare_duplicate_plans_with_same_price_are_fine(applecare_item):
    plan = {"partNumber": "SXKK2HN/A", "rawPrice": 26900.0, **FULL}

    assert parse(_bootstrap([plan, dict(plan)]), applecare_item).price_inr == 26900


def _ld(offers: list[dict]) -> str:
    payload = json.dumps({"@type": "Product", "offers": offers})
    return f'<script type="application/ld+json">{payload}</script>'


def test_device_duplicate_offers_with_different_prices_are_ambiguous(live_device_item):
    sku = live_device_item["expected_sku"]
    html = _ld(
        [
            {"priceCurrency": "INR", "price": 1, "sku": sku},
            {"priceCurrency": "INR", "price": 251900, "sku": sku},
        ]
    )

    result = parse(html, live_device_item)

    assert result.status == STATUS_FAILED
    assert result.price_inr is None
    assert "ambiguous" in result.error


def test_device_duplicate_offers_with_same_price_are_fine(live_device_item):
    offer = {"priceCurrency": "INR", "price": "251900.00", "sku": live_device_item["expected_sku"]}

    assert parse(_ld([offer, dict(offer)]), live_device_item).price_inr == 251900


@pytest.mark.parametrize("currency", [None, "USD"])
def test_device_missing_or_foreign_currency_fails(live_device_item, currency):
    offer = {"price": 251900, "sku": live_device_item["expected_sku"]}
    if currency:
        offer["priceCurrency"] = currency

    result = parse(_ld([offer]), live_device_item)

    assert result.status == STATUS_FAILED
    assert result.price_inr is None
    assert "currency" in result.error.lower()


def test_applecare_without_bootstrap_fails(applecare_item):
    result = parse("<html><body>hello world</body></html>", applecare_item)

    assert result.status == STATUS_FAILED
    assert "APPLECARE_BOOTSTRAP" in result.error


# ---------------------------------------------------------------- traps


def test_combined_headline_price_is_never_returned(fixture_html, config_items, device_item):
    for item in [*config_items.values(), device_item]:
        result = parse(fixture_html, item)
        assert result.price_inr not in (COMBINED_PRICE, COMBINED_MRP)
        assert result.mrp_inr not in (COMBINED_PRICE, COMBINED_MRP)


@pytest.mark.parametrize(
    "html",
    [
        "<html><head><title>Access Denied</title></head><body>Reference #18.abc</body></html>",
        "<html><body>Please complete the CAPTCHA to continue</body></html>",
        "<html><body>Our systems have detected unusual traffic from your network.</body></html>",
    ],
)
def test_blocked_pages_are_detected(html, device_item, applecare_item):
    assert parse(html, device_item).status == STATUS_BLOCKED
    assert parse(html, applecare_item).status == STATUS_BLOCKED


@pytest.mark.parametrize(
    "marker", ["700009", "chrome-extension://", "bh-crx", "hk-offersReadyPopCont", "saved from url"]
)
def test_public_fixtures_contain_no_personal_or_extension_data(fixture_html, live_raw_html, marker):
    assert marker not in fixture_html
    assert marker not in live_raw_html


def test_empty_page_fails(device_item):
    assert parse("", device_item).status == STATUS_FAILED


def test_unknown_line_item_fails(fixture_html, device_item):
    assert parse(fixture_html, {**device_item, "line_item": "accessory"}).status == STATUS_FAILED
