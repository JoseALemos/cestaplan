"""Parse.bot chain mappers (Alcampo/Carrefour/Aldi/Lidl) — offline, synthetic only, no network.

Per-record scenarios call ``map_product`` directly (like the DIA suite); schema pinning is
checked separately with the versioned synthetic fixtures (whose structure equals the observed
capture) and with a deliberate type-drift. No real data, no network.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest

from cestaplan_api.config import Settings
from cestaplan_api.ingestion.contracts import PriceScope
from cestaplan_api.ingestion.providers.contracts import Availability, ContentUnit
from cestaplan_api.ingestion.providers.onboarding import measure_coverage
from cestaplan_api.ingestion.providers.parsebot import plans
from cestaplan_api.ingestion.providers.parsebot.chains import (
    ParseBotAlcampoMapper,
    ParseBotAldiMapper,
    ParseBotCarrefourMapper,
    ParseBotLidlMapper,
    UnsupportedSchemaError,
)
from cestaplan_api.ingestion.providers.parsebot.client import ParseBotClient

_NOW = datetime(2026, 7, 23, 12, 0, tzinfo=UTC)
_FIX = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "providers"


def _fixture(provider: str) -> list[dict]:
    return json.loads((_FIX / provider / "v1.synthetic.json").read_text())


# --------------------------------------------------------------------------- #
# Fixture-driven pinning: each versioned synthetic fixture maps end to end.
# --------------------------------------------------------------------------- #
def test_fixtures_match_pinned_fingerprint_and_map() -> None:
    for provider, mapper in (
        ("parsebot-alcampo", ParseBotAlcampoMapper()),
        ("parsebot-carrefour", ParseBotCarrefourMapper()),
        ("parsebot-aldi", ParseBotAldiMapper()),
        ("parsebot-lidl", ParseBotLidlMapper()),
    ):
        records = _fixture(provider)
        fp = mapper.detect_schema(records)
        assert fp in mapper.supported_schema_fingerprints, provider
        products = mapper.map_products(records, retrieved_at=_NOW)
        assert len(products) == len(records)
        assert all(p.regular_price is not None for p in products)
        assert all(isinstance(p.regular_price, Decimal) for p in products)  # never float


def test_costing_eligibility_matches_intent() -> None:
    # Alcampo (net content) is costable; Aldi (textual size) is not — measured, not declared.
    alc = ParseBotAlcampoMapper().map_products(_fixture("parsebot-alcampo"), retrieved_at=_NOW)
    cov = measure_coverage(
        alc, captured=len(alc), limit=10, supports_full_catalog=False, supports_store_scope=False
    )
    assert cov.observed_catalog_scope == "sample_only"  # never "full" from a sample
    assert cov.costing_eligibility == "sufficient"

    aldi = ParseBotAldiMapper().map_products(_fixture("parsebot-aldi"), retrieved_at=_NOW)
    covodd = measure_coverage(
        aldi, captured=len(aldi), limit=10, supports_full_catalog=False, supports_store_scope=False
    )
    assert covodd.costing_eligibility == "insufficient"


# --------------------------------------------------------------------------- #
# Alcampo
# --------------------------------------------------------------------------- #
def _alcampo(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "productId": "uuid-1",
        "retailerProductId": "54178",
        "name": "Leche entera 6 x 1 L",
        "brand": "PRODUCTO ALCAMPO",
        "categoryPath": ["Lácteos", "Leche", "Leche entera"],
        "price": {"amount": "5.70", "currency": "EUR"},
        "unitPrice": {"price": {"amount": "0.95", "currency": "EUR"}, "unitName": "PER_LITRE"},
        "packSizeDescription": "6000ml",
        "available": True,
        "type": "REGULAR",
    }
    base.update(over)
    return base


def test_alcampo_normal_is_costable() -> None:
    p = ParseBotAlcampoMapper().map_product(_alcampo(), _NOW)
    assert p.external_product_id == "54178"
    assert p.regular_price == Decimal("5.70") and p.currency == "EUR"
    assert p.net_content_quantity == Decimal("6000") and p.net_content_unit is ContentUnit.ML
    assert p.unit_price == Decimal("0.95") and p.unit_price_unit == "l"
    assert p.barcode is None  # never invented
    assert p.price_scope is PriceScope.UNKNOWN


def test_alcampo_no_brand_and_ambiguous_size() -> None:
    p = ParseBotAlcampoMapper().map_product(_alcampo(brand="", packSizeDescription="pack"), _NOW)
    assert p.brand is None
    assert p.net_content_quantity is None and p.net_content_unit is None  # not guessed


def test_alcampo_out_of_stock_and_bad_price() -> None:
    assert (
        ParseBotAlcampoMapper().map_product(_alcampo(available=False), _NOW).availability
        is Availability.OUT_OF_STOCK
    )
    with pytest.raises(UnsupportedSchemaError):
        ParseBotAlcampoMapper().map_product(_alcampo(price={"amount": "", "currency": "EUR"}), _NOW)


def test_alcampo_unknown_fingerprint_blocks() -> None:
    drift = _alcampo()
    drift["price"] = "5.70"  # object -> string: structural drift
    with pytest.raises(UnsupportedSchemaError):
        ParseBotAlcampoMapper().map_products([drift], retrieved_at=_NOW)


def test_alcampo_empty_maps_to_nothing() -> None:
    assert ParseBotAlcampoMapper().map_products([], retrieved_at=_NOW) == []


# --------------------------------------------------------------------------- #
# Carrefour
# --------------------------------------------------------------------------- #
def _carrefour(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "product_id": "529921745",
        "name": "Banana a granel 1 Kg aprox",
        "brand": "SIN MARCA",
        "category": None,
        "regular_price": 1.49,
        "promotional_price": None,
        "loyalty_price": None,
        "measure_unit": "kg",
        "package_quantity": 1,
        "package_unit": None,
        "net_content": None,
        "unit_price": 1.49,
        "unit_price_unit": "kg",
        "availability": "in_stock",
        "ean": None,
        "postal_code": "14007",
        "sale_point": "005290",
        "observed_at": "2026-07-23T20:29:39+00:00",
        "promotion_text": None,
        "promotion_start_date": None,
        "promotion_end_date": None,
    }
    base.update(over)
    return base


def test_carrefour_weight_priced_is_costable() -> None:
    p = ParseBotCarrefourMapper().map_product(_carrefour(), _NOW)
    assert p.variable_weight is True
    assert p.unit_price == Decimal("1.49") and p.unit_price_unit == "kg"
    assert p.price_scope is PriceScope.POSTAL_CODE and p.postal_code == "14007"
    assert p.external_store_id == "005290"
    assert p.observed_at == datetime(2026, 7, 23, 20, 29, 39, tzinfo=UTC)  # source timestamp


def test_carrefour_barcode_and_promotion() -> None:
    p = ParseBotCarrefourMapper().map_product(
        _carrefour(ean="8410000000001", regular_price=2.0, promotional_price=1.5), _NOW
    )
    assert p.barcode == "8410000000001"
    assert p.promotional_price == Decimal("1.5") and p.regular_price == Decimal("2.0")
    assert p.promotion is not None and p.promotion.percentage_discount == Decimal("25.00")


def test_carrefour_packaged_net_content() -> None:
    p = ParseBotCarrefourMapper().map_product(
        _carrefour(net_content="500g", package_unit="g", package_quantity=500, measure_unit="ud"),
        _NOW,
    )
    assert p.net_content_quantity == Decimal("500") and p.net_content_unit is ContentUnit.G
    assert p.variable_weight is False


def test_carrefour_missing_price_raises() -> None:
    with pytest.raises(UnsupportedSchemaError):
        ParseBotCarrefourMapper().map_product(
            _carrefour(regular_price=None, promotional_price=None), _NOW
        )


# --------------------------------------------------------------------------- #
# Aldi (weekly offers)
# --------------------------------------------------------------------------- #
def _aldi(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "product_id": "602164200",
        "title": "Nectarina",
        "brand": None,
        "category": None,
        "displayed_price": 1.99,
        "previous_price": 2.79,
        "package_size": "precio kg (a granel)",
        "promotion_text": "-28%",
        "region": "peninsula",
        "observation_timestamp": "2026-07-23T20:27:44+00:00",
        "valid_from": "2026-07-20",
        "valid_until": "2026-07-26",
        "image_url": "https://aldi/x.jpg",
        "product_url": "https://aldi/p/x",
    }
    base.update(over)
    return base


def test_aldi_markdown_offer() -> None:
    p = ParseBotAldiMapper().map_product(_aldi(), _NOW)
    assert p.regular_price == Decimal("2.79") and p.promotional_price == Decimal("1.99")
    assert p.promotion is not None and p.promotion.percentage_discount == Decimal("28")
    assert p.variable_weight is True  # "granel"
    assert p.observed_at == datetime(2026, 7, 23, 20, 27, 44, tzinfo=UTC)


def test_aldi_no_markdown_and_currency_inferred() -> None:
    p = ParseBotAldiMapper().map_product(_aldi(previous_price=None, package_size="unidad"), _NOW)
    assert p.promotional_price is None and p.regular_price == Decimal("1.99")
    assert p.currency == "EUR"  # inferred from ES source, recorded in raw_source_reference
    assert "inferred:ES" in (p.raw_source_reference or "")


def test_aldi_missing_price_raises() -> None:
    with pytest.raises(UnsupportedSchemaError):
        ParseBotAldiMapper().map_product(_aldi(displayed_price=None), _NOW)


def test_aldi_plan_extracts_offers_envelope_and_maps() -> None:
    # Drive the real capture plan (endpoint + list extraction) against a mocked client, using
    # the versioned fixture batch so the pinned fingerprint validates offline (no network).
    offers = _fixture("parsebot-aldi")

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "success", "data": {"offers": offers}})

    client = ParseBotClient(
        base_url="https://api.parse.bot/scraper/aldi",
        api_key="k",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    settings = Settings(parse_bot_api_key="k", parse_bot_aldi_base_url="https://x/scraper/aldi")
    records = plans.capture_records("parsebot-aldi", settings, limit=10, client=client)
    products = ParseBotAldiMapper().map_products(records, retrieved_at=_NOW)
    assert len(products) == len(offers)


# --------------------------------------------------------------------------- #
# Lidl (store-scoped visible products)
# --------------------------------------------------------------------------- #
def _lidl(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "product_id": "11115615",
        "name": "Leche semidesnatada",
        "full_title": "Milbona Leche semidesnatada",
        "brand": "Milbona",
        "category": "Lácteos",
        "price": 0.99,
        "currency": "EUR",
        "old_price": None,
        "discount_percentage": None,
        "promotion": None,
        "packaging": "1000ml",
        "observed_at": "2026-07-23T20:34:49+00:00",
        "store_id": "ES00549",
        "image": "https://lidl/x.jpg",
        "product_url": "https://lidl/p/x",
    }
    base.update(over)
    return base


def test_lidl_store_scoped_with_packaging() -> None:
    p = ParseBotLidlMapper().map_product(_lidl(), _NOW)
    assert p.currency == "EUR" and p.regular_price == Decimal("0.99")
    assert p.net_content_quantity == Decimal("1000") and p.net_content_unit is ContentUnit.ML
    assert p.price_scope is PriceScope.EXACT_STORE and p.external_store_id == "ES00549"


def test_lidl_markdown() -> None:
    p = ParseBotLidlMapper().map_product(
        _lidl(price=0.79, old_price=0.99, discount_percentage=20), _NOW
    )
    assert p.regular_price == Decimal("0.99") and p.promotional_price == Decimal("0.79")
    assert p.promotion is not None and p.promotion.percentage_discount == Decimal("20")


def test_lidl_missing_price_raises() -> None:
    with pytest.raises(UnsupportedSchemaError):
        ParseBotLidlMapper().map_product(_lidl(price=None), _NOW)
