from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"
FIXTURE_HTML = FIXTURES / "apple_edu_mbp.html"  # browser-rendered, 96W adapter selected
LIVE_RAW_HTML = FIXTURES / "apple_live_raw.html"  # exact httpx response bytes, URL default (70W)

SKU_70W = "Z1KM+CK56+CK58+CK5F+CK5H+CK5M+CK5P+CK66+CK77+CK7K+HNCK5Q"
SKU_96W = "Z1KM+CK56+CK58+CK5F+CK5J+CK5M+CK5P+CK66+CK77+CK7K+HNCK5Q"


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests never see real Telegram credentials or a forced summary from the developer's shell."""
    for name in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "FORCE_SUMMARY"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(scope="session")
def fixture_html() -> str:
    return FIXTURE_HTML.read_text(encoding="utf-8")


@pytest.fixture(scope="session")
def live_raw_html() -> str:
    return LIVE_RAW_HTML.read_bytes().decode("utf-8")


@pytest.fixture(scope="session")
def config_items() -> dict[str, dict]:
    with (ROOT / "config.json").open("r", encoding="utf-8") as f:
        return {item["id"]: item for item in json.load(f)["items"]}


@pytest.fixture
def live_device_item(config_items: dict[str, dict]) -> dict:
    """The device item exactly as configured (URL-default 70W sku)."""
    return dict(config_items["m5_mbp14_silver_24_1tb"])


@pytest.fixture
def device_item(live_device_item: dict) -> dict:
    """Test-only variant matching the browser-rendered fixture (96W adapter selected)."""
    return {**live_device_item, "expected_sku": SKU_96W}


@pytest.fixture
def applecare_item(config_items: dict[str, dict]) -> dict:
    return dict(config_items["m5_mbp14_silver_24_1tb_applecare"])
