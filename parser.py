"""Pure HTML -> price parser for Apple Store (India education) buy pages.

No network access. Prefers structured data embedded in the page:
  * device:    Product JSON-LD offers[].price, matched on part-number prefix AND full sku
  * applecare: window.APPLECARE_BOOTSTRAP planDetails[].rawPrice, matched on partNumber

The page headline price is device + AppleCare+ combined and is never used.
"""

from __future__ import annotations

import html as html_lib
import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

STATUS_OK = "ok"
STATUS_FAILED = "failed"
STATUS_BLOCKED = "blocked"
STATUS_NO_MATCH = "no_match"
STATUS_SUSPECT = "suspect"  # parsed fine but implausible (see sanity.py); never used as a price
FULL_PRICE = "fullprice"

MAX_OFFER_TEXTS = 3
MAX_OFFER_TEXT_LEN = 400

# Phrases that only appear on bot-check / error interstitials, not on normal store pages.
_BLOCK_PATTERNS = re.compile(
    r"captcha|access denied|are you a robot|unusual traffic|too many requests"
    r"|request blocked|pardon our interruption|verify you are (a )?human",
    re.IGNORECASE,
)
_JSON_LD_RE = re.compile(
    r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', re.IGNORECASE | re.DOTALL
)
_MRP_ITEM_RE = re.compile(
    r'<div class="rc-mrp-item-title">(.*?)</div>\s*<div class="rc-mrp-item-price">(.*?)</div>',
    re.DOTALL,
)
_MRP_AMOUNT_RE = re.compile(r"MRP\s*\D{0,8}?([\d,]+(?:\.\d+)?)")
_FORM_RE = re.compile(r"<form\b([^>]*)>(.*?)</form>", re.IGNORECASE | re.DOTALL)
_ADD_TO_CART_RE = re.compile(
    r'<button[^>]*type="submit"[^>]*name="add-to-cart"|<button[^>]*name="add-to-cart"[^>]*type="submit"',
    re.IGNORECASE,
)
_ACPART_INPUT_RE = re.compile(r'<input\b[^>]*\bname="acpart"[^>]*>', re.IGNORECASE)
_VALUE_ATTR_RE = re.compile(r'\bvalue="([^"]*)"')
_TEXT_NODE_RE = re.compile(r">([^<>]{10,2000})<")
_OFFER_TEXT_RE = re.compile(r"\bfree\b.*\btrial\b|\bmonths? free\b", re.IGNORECASE | re.DOTALL)
_APPLECARE_BOOTSTRAP_RE = re.compile(r"window\.APPLECARE_BOOTSTRAP\s*=\s*\{\s*appleCareData\s*:\s*")
_APPLECARE_PRICE_MAP_RE = re.compile(r'"appleCaresPriceMap"\s*:\s*')


class ParseError(Exception):
    """Expected data is missing or malformed."""


@dataclass(frozen=True)
class ParseResult:
    status: str
    price_inr: int | None = None
    mrp_inr: int | None = None
    part_number_matched: str | None = None
    in_stock: bool | None = None
    offers_raw: str | None = None
    error: str | None = None


def parse(html: str, item: dict[str, Any]) -> ParseResult:
    """Extract price data for one configured item from a page's HTML."""
    try:
        if not html or not html.strip():
            return ParseResult(status=STATUS_FAILED, error="empty page")
        products = _json_ld_products(html)
        if not products and is_blocked_page(html):
            return ParseResult(status=STATUS_BLOCKED, error="bot-check or block page detected")

        line_item = item.get("line_item")
        if line_item == "device":
            return _parse_device(html, item, products)
        if line_item == "applecare":
            return _parse_applecare(html, item)
        return ParseResult(status=STATUS_FAILED, error=f"unknown line_item {line_item!r}")
    except ParseError as exc:
        return ParseResult(status=STATUS_FAILED, error=str(exc))


def is_blocked_page(html: str) -> bool:
    return bool(_BLOCK_PATTERNS.search(html))


# --------------------------------------------------------------------------- device


def _parse_device(html: str, item: dict[str, Any], products: list[dict]) -> ParseResult:
    if not products:
        raise ParseError("Product JSON-LD not found")

    part_number = item["part_number"]
    expected_sku = item.get("expected_sku")
    offers = [o for p in products for o in _as_list(p.get("offers")) if isinstance(o, dict)]
    skus_seen = [str(o.get("sku")) for o in offers]

    matches = [o for o in offers if _sku_matches(o.get("sku"), part_number, expected_sku)]
    if not matches:
        wanted = expected_sku or f"{part_number}+..."
        return ParseResult(
            status=STATUS_NO_MATCH,
            error=f"no JSON-LD offer with sku {wanted}; page skus: {', '.join(skus_seen) or 'none'}",
        )
    for offer in matches:
        currency = offer.get("priceCurrency")
        if currency is None:
            raise ParseError("JSON-LD offer has no priceCurrency")
        if currency != "INR":
            raise ParseError(f"unexpected currency {currency!r}")

    return ParseResult(
        status=STATUS_OK,
        price_inr=_single_price([_to_rupees(o.get("price")) for o in matches], "JSON-LD offers"),
        mrp_inr=_mrp_from_summary(html, item.get("mrp_title")),
        part_number_matched=part_number,
        in_stock=_in_stock(html, part_number=part_number),
        offers_raw=_offers_raw(html),
    )


def _sku_matches(sku: Any, part_number: str, expected_sku: str | None) -> bool:
    if not isinstance(sku, str) or sku.split("+", 1)[0] != part_number:
        return False
    return expected_sku is None or sku == expected_sku


def _json_ld_products(html: str) -> list[dict]:
    products: list[dict] = []
    for raw in _JSON_LD_RE.findall(html):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        for node in _as_list(data):
            if isinstance(node, dict) and node.get("@type") == "Product":
                products.append(node)
    return products


# ------------------------------------------------------------------------ applecare


def _parse_applecare(html: str, item: dict[str, Any]) -> ParseResult:
    part_number = item["part_number"]

    page_acpart = _form_acpart(html)
    if page_acpart is not None and page_acpart != part_number:
        return ParseResult(
            status=STATUS_NO_MATCH,
            error=f"page acpart is {page_acpart}, configured {part_number}",
        )

    plans = _applecare_plans(html)
    if plans is None:
        raise ParseError("APPLECARE_BOOTSTRAP not found")
    matching = [
        p
        for p in plans
        if p.get("partNumber") == part_number and "rawPrice" in p and _payment_type(p) == FULL_PRICE
    ]
    if not matching:
        found = sorted({str(p.get("partNumber")) for p in plans if "partNumber" in p})
        return ParseResult(
            status=STATUS_NO_MATCH,
            error=(
                f"no full-price AppleCare plan {part_number}; "
                f"page plans: {', '.join(found) or 'none'}"
            ),
        )
    price = _single_price([_to_rupees(p["rawPrice"]) for p in matching], "AppleCare+ plans")

    mrp = _mrp_from_summary(html, item.get("mrp_title"))
    if mrp is None:
        mrp = _applecare_mrp_from_price_map(html, part_number)

    return ParseResult(
        status=STATUS_OK,
        price_inr=price,
        mrp_inr=mrp,
        part_number_matched=part_number,
        in_stock=_in_stock(html, acpart=part_number),
        offers_raw=_offers_raw(html),
    )


def _applecare_plans(html: str) -> list[dict] | None:
    """All dicts carrying a partNumber inside window.APPLECARE_BOOTSTRAP.appleCareData."""
    m = _APPLECARE_BOOTSTRAP_RE.search(html)
    if not m:
        return None
    try:
        data, _ = json.JSONDecoder().raw_decode(html, m.end())
    except json.JSONDecodeError as exc:
        raise ParseError(f"APPLECARE_BOOTSTRAP is not valid JSON: {exc}") from exc
    return [d for d in _walk_dicts(data) if "partNumber" in d]


def _payment_type(plan: dict) -> str | None:
    dpt = plan.get("dimensionPaymentType")
    return dpt.get("value") if isinstance(dpt, dict) else None


def _form_acpart(html: str) -> str | None:
    m = _ACPART_INPUT_RE.search(html)
    if not m:
        return None
    v = _VALUE_ATTR_RE.search(m.group(0))
    return html_lib.unescape(v.group(1)) if v else None


def _applecare_mrp_from_price_map(html: str, part_number: str) -> int | None:
    m = _APPLECARE_PRICE_MAP_RE.search(html)
    if not m:
        return None
    try:
        price_map, _ = json.JSONDecoder().raw_decode(html, m.end())
    except json.JSONDecodeError:
        return None
    entry = price_map.get(part_number) if isinstance(price_map, dict) else None
    if not isinstance(entry, dict):
        return None
    return _mrp_amount(str(entry.get("fullMrpPrice", "")))


# --------------------------------------------------------------------------- shared


def _mrp_from_summary(html: str, title: str | None) -> int | None:
    """MRP from the 'Show MRP details' block, matched by exact (normalized) item title."""
    if not title:
        return None
    wanted = _normalize_text(title)
    for raw_title, raw_price in _MRP_ITEM_RE.findall(html):
        if _normalize_text(raw_title) == wanted:
            return _mrp_amount(raw_price)
    return None


def _mrp_amount(text: str) -> int | None:
    m = _MRP_AMOUNT_RE.search(html_lib.unescape(text))
    return _to_rupees(m.group(1).replace(",", "")) if m else None


def _in_stock(html: str, *, part_number: str | None = None, acpart: str | None = None) -> bool | None:
    """True if an Add to Bag form for this item is present; None if undetermined."""
    for attrs, body in _FORM_RE.findall(html):
        if not _ADD_TO_CART_RE.search(body):
            continue
        if part_number and f'data-part-number="{part_number}"' in attrs:
            return True
        if acpart and _form_acpart(body) == acpart:
            return True
    return None


def _offers_raw(html: str) -> str | None:
    """Raw promo text (e.g. free-trial offers). Informational only; never affects price."""
    texts: list[str] = []
    for raw in _TEXT_NODE_RE.findall(html):
        text = _normalize_text(raw.replace("\\n", " ").replace('\\"', '"'))
        if not _OFFER_TEXT_RE.search(text) or len(text) > MAX_OFFER_TEXT_LEN:
            continue
        if text not in texts:
            texts.append(text)
        if len(texts) >= MAX_OFFER_TEXTS:
            break
    return " | ".join(texts) or None


def _single_price(prices: list[int], source: str) -> int:
    """The one price all matching entries agree on; disagreement is an error, not a guess."""
    distinct = sorted(set(prices))
    if len(distinct) != 1:
        raise ParseError(f"ambiguous: {len(prices)} matching {source} with different prices {distinct}")
    return distinct[0]


def _to_rupees(value: Any) -> int:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ParseError(f"invalid price value {value!r}") from exc
    if not amount.is_finite() or amount <= 0:
        raise ParseError(f"invalid price value {value!r}")
    return int(amount.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def _normalize_text(text: str) -> str:
    return " ".join(html_lib.unescape(text).replace("\xa0", " ").split())


def _as_list(value: Any) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _walk_dicts(node: Any) -> Iterator[dict]:
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk_dicts(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk_dicts(v)
