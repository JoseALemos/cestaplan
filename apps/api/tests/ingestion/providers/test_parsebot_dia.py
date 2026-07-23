"""Parse.bot DIA mapper (spec §5-§8) — offline, synthetic records only, no network.

Every case from §8 using synthetic DIA-shaped records (no real data): normal, promo, no
brand, no barcode (always), by weight/volume/unit, out of stock, null optionals, decimal
prices, unknown unit, missing price/id, unknown fingerprint, empty response, schema drift,
unknown scope and ambiguous promotion. Also a provider-level parse via a mocked client.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from cestaplan_api.ingestion.contracts import PriceScope
from cestaplan_api.ingestion.providers.contracts import Availability, ProductQuery, SellUnit
from cestaplan_api.ingestion.providers.parsebot.client import ParseBotClient
from cestaplan_api.ingestion.providers.parsebot.dia import (
    ParseBotDiaMapper,
    ParseBotDiaProvider,
    UnsupportedSchemaError,
)
from cestaplan_api.ingestion.providers.parsebot.schemas import ParseBotDiaProduct

_NOW = datetime(2026, 7, 23, 12, 0, tzinfo=UTC)


def _prices(**over: Any) -> dict[str, Any]:
    base = {
        "currency": "EUR",
        "price": 1.15,
        "strikethrough_price": 1.15,
        "price_per_unit": 1.15,
        "measure_unit": "LITRO",
        "discount_percentage": 0,
        "is_promo_price": False,
        "is_club_price": False,
    }
    base.update(over)
    return base


def _raw(**over: Any) -> dict[str, Any]:
    base = {
        "sku_id": "SKU1",
        "display_name": "Leche entera 1 L",
        "brand": "Marca",
        "brand_type": "national",
        "l1_category_description": "Lácteos",
        "l2_category_description": "Leche",
        "image": "https://img/1.jpg",
        "url": "https://dia/p/1",
        "object_id": "OBJ1",
        "units_in_stock": 5,
        "units_in_cart": 0,
        "prices": _prices(),
    }
    base.update(over)
    return base


def _map_one(raw: dict[str, Any]) -> Any:
    return ParseBotDiaMapper().map_product(ParseBotDiaProduct.model_validate(raw), _NOW)


def test_normal_product() -> None:
    p = _map_one(_raw())
    assert p.external_product_id == "SKU1"
    assert p.product_name == "Leche entera 1 L"
    assert p.regular_price == Decimal("1.15")
    assert isinstance(p.regular_price, Decimal)  # no float
    assert p.currency == "EUR"
    assert p.barcode is None  # never invented
    assert p.net_content_quantity is None and p.net_content_unit is None  # §7
    assert p.price_scope is PriceScope.UNKNOWN  # §6
    assert p.observed_at == _NOW  # retrieval time
    assert "source_observed_at=absent" in (p.raw_source_reference or "")


def test_promotion_uses_strikethrough_as_regular() -> None:
    p = _map_one(
        _raw(
            prices=_prices(
                is_promo_price=True, price=0.99, strikethrough_price=1.30, discount_percentage=24
            )
        )
    )
    assert p.regular_price == Decimal("1.30")
    assert p.promotional_price == Decimal("0.99")
    assert p.promotion is not None and p.promotion.percentage_discount == Decimal("24")


def test_ambiguous_promo_not_read_as_regular() -> None:
    # flagged promo but strikethrough is NOT above price -> do not fabricate a markdown
    p = _map_one(_raw(prices=_prices(is_promo_price=True, price=1.15, strikethrough_price=1.15)))
    assert p.regular_price == Decimal("1.15")
    assert p.promotional_price is None
    assert p.promotion is None


def test_club_price_is_loyalty() -> None:
    p = _map_one(_raw(prices=_prices(is_club_price=True, price=1.00)))
    assert p.loyalty_price == Decimal("1.00")


def test_no_brand() -> None:
    assert _map_one(_raw(brand=""))  # accepted; brand normalised to None
    assert _map_one(_raw(brand="")).brand is None


def test_units_volume_weight_unknown() -> None:
    assert _map_one(_raw(prices=_prices(measure_unit="LITRO"))).unit_price_unit == "l"
    assert _map_one(_raw(prices=_prices(measure_unit="KILO"))).unit_price_unit == "kg"
    assert _map_one(_raw(prices=_prices(measure_unit="UNIDAD"))).unit_price_unit == "unit"
    # unknown unit word -> no unit price fabricated
    unknown = _map_one(_raw(prices=_prices(measure_unit="CAJA")))
    assert unknown.unit_price is None and unknown.unit_price_unit is None


def test_out_of_stock() -> None:
    assert _map_one(_raw(units_in_stock=0)).availability is Availability.OUT_OF_STOCK
    assert _map_one(_raw(units_in_stock=3)).availability is Availability.IN_STOCK


def test_null_optionals_ok() -> None:
    p = _map_one(_raw())  # no dia_brand/allergens/product_info
    assert p is not None


def test_missing_critical_fields_raise() -> None:
    bad = _raw()
    del bad["sku_id"]
    with pytest.raises(ValidationError):
        ParseBotDiaProduct.model_validate(bad)
    bad2 = _raw()
    del bad2["prices"]["price"]
    with pytest.raises(ValidationError):
        ParseBotDiaProduct.model_validate(bad2)


def test_empty_response_maps_to_nothing() -> None:
    assert ParseBotDiaMapper().map_products([], retrieved_at=_NOW) == []


def test_unknown_fingerprint_blocks_normalization() -> None:
    drifted = _raw()
    drifted["prices"]["price"] = "1.15"  # type change number->string -> different core structure
    with pytest.raises(UnsupportedSchemaError):
        ParseBotDiaMapper().map_products([drifted], retrieved_at=_NOW)


def test_supported_fingerprint_batch_maps() -> None:
    products = ParseBotDiaMapper().map_products([_raw(), _raw(sku_id="SKU2")], retrieved_at=_NOW)
    assert [p.external_product_id for p in products] == ["SKU1", "SKU2"]


# --- provider-level parse via a mocked client (pagination envelope) -------- #
def test_provider_parses_search_items_envelope() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {
                    "pagination": {"page_number": 1, "page_size": 30, "total_pages": 1},
                    "total_items": 2,
                    "search_items": [_raw(), _raw(sku_id="SKU2")],
                },
            },
        )

    client = ParseBotClient(
        base_url="https://api.parse.bot/scraper/dia",
        api_key="k",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    provider = ParseBotDiaProvider(client=client)
    products = list(provider.iterate_products(ProductQuery(max_products=10)))
    assert [p.external_product_id for p in products] == ["SKU1", "SKU2"]
    assert all(p.price_scope is PriceScope.UNKNOWN for p in products)
    assert all(p.sell_unit is SellUnit.PACKAGE for p in products)
