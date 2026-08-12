"""DIA DIRECT public-API connector (transport + mapper + provider) — fully offline.

No network is ever touched: an ``httpx.MockTransport`` serves the paginated search endpoint and
``sleep``/``delay`` are injected so courtesy pacing is asserted without real waiting. Covered: net
content parsed from ``display_name`` (single + pack) drives FIXED_PACKAGE costing; a non-parseable
name falls back to unit-price costing; a non-positive price is skipped (tolerant, counted); the
paginated walk respects ``total_pages``; a 403/429 block signal stops the walk; the gating turns
the provider on/off; ``search=True`` / ``full_catalog=False`` / ``store_scope=False`` capabilities;
and the monthly scheduler cadence.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
import pytest

from cestaplan_api.config import Settings
from cestaplan_api.ingestion.contracts import PriceScope, RunType
from cestaplan_api.ingestion.providers.contracts import (
    Availability,
    ContentUnit,
    ProductQuery,
    SellUnit,
)
from cestaplan_api.ingestion.providers.dia.client import DiaClient
from cestaplan_api.ingestion.providers.dia.mapping import DiaMapper
from cestaplan_api.ingestion.providers.dia.provider import DiaProvider
from cestaplan_api.ingestion.providers.exceptions import NotSupportedError, ProviderResponseError
from cestaplan_api.ingestion.scheduler import SchedulerConfig

_NOW = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)
_UA = "CestaPlan/1.0 price-sync (+https://cestaplan)"


def _prices(**over: Any) -> dict[str, Any]:
    base = {
        "price": 4.19,
        "price_per_unit": 0.70,
        "measure_unit": "LITRO",
        "strikethrough_price": None,
        "is_promo_price": False,
        "discount_percentage": 0,
        "currency": "EUR",
    }
    base.update(over)
    return base


def _item(**over: Any) -> dict[str, Any]:
    base = {
        "sku_id": "504P6",
        "object_id": "504P6",
        "display_name": "Leche semidesnatada Dia Láctea pack 6 x 1 L",
        "brand": "Dia",
        "brand_type": "own",
        "dia_brand": True,
        "units_in_stock": 10,
        "url": "https://www.dia.es/p/504P6",
        "image": "https://img/504P6.jpg",
        "l1_category_description": "Lácteos",
        "l2_category_description": "Leche",
        "prices": _prices(),
    }
    base.update(over)
    return base


# --- mapper: net content, fallback, tolerance ---------------------------- #
def test_pack_net_content_drives_fixed_package() -> None:
    [p] = DiaMapper().map_products([_item()], observed_at=_NOW)
    assert p.external_product_id == "504P6"
    assert p.product_name == "Leche semidesnatada Dia Láctea pack 6 x 1 L"
    assert p.brand == "Dia"
    assert p.regular_price == Decimal("4.19")
    assert isinstance(p.regular_price, Decimal)  # never a float
    assert p.net_content_quantity == Decimal("6") and p.net_content_unit is ContentUnit.L
    assert p.price_scope is PriceScope.NATIONAL and p.postal_code is None
    assert p.sell_unit is SellUnit.PACKAGE
    assert p.availability is Availability.IN_STOCK
    assert p.currency == "EUR"
    assert p.barcode is None  # never invented


def test_single_net_content_parsed() -> None:
    [p] = DiaMapper().map_products(
        [_item(display_name="Aceite de oliva virgen extra Dia 1 L")], observed_at=_NOW
    )
    assert p.net_content_quantity == Decimal("1") and p.net_content_unit is ContentUnit.L
    [q] = DiaMapper().map_products(
        [_item(display_name="Harina de trigo Dia 500 g")], observed_at=_NOW
    )
    assert q.net_content_quantity == Decimal("500") and q.net_content_unit is ContentUnit.G


def test_unparseable_name_falls_back_to_unit_price() -> None:
    # No mass/volume net content in the name -> net content dropped, unit price kept from the
    # response (parsebot-dia is a UNIT_PRICE_COSTED provider, so this stays costable).
    [p] = DiaMapper().map_products(
        [
            _item(
                display_name="Papel higiénico Dia 12 rollos",
                prices=_prices(measure_unit="UNIDAD", price_per_unit=0.30),
            )
        ],
        observed_at=_NOW,
    )
    assert p.net_content_quantity is None and p.net_content_unit is None
    assert p.unit_price == Decimal("0.30") and p.unit_price_unit == "unit"


def test_non_positive_price_is_skipped_and_counted() -> None:
    mapper = DiaMapper()
    products = mapper.map_products(
        [_item(sku_id="OK"), _item(sku_id="BAD", prices=_prices(price=0))], observed_at=_NOW
    )
    assert [p.external_product_id for p in products] == ["OK"]  # the free/negative one is dropped
    assert mapper.last_skipped_count == 1


def test_promotion_reads_strikethrough_as_regular() -> None:
    [p] = DiaMapper().map_products(
        [_item(prices=_prices(price=3.49, strikethrough_price=4.19, is_promo_price=True))],
        observed_at=_NOW,
    )
    assert p.regular_price == Decimal("4.19")
    assert p.promotional_price == Decimal("3.49")
    assert p.promotion is not None


def test_ambiguous_promo_not_read_as_markdown() -> None:
    [p] = DiaMapper().map_products(
        [_item(prices=_prices(price=4.19, strikethrough_price=4.19, is_promo_price=True))],
        observed_at=_NOW,
    )
    assert p.regular_price == Decimal("4.19") and p.promotional_price is None


def test_malformed_record_is_skipped_never_raises() -> None:
    mapper = DiaMapper()
    products = mapper.map_products(
        [_item(sku_id="OK"), {"display_name": "no sku or prices"}], observed_at=_NOW
    )
    assert [p.external_product_id for p in products] == ["OK"]
    assert mapper.last_skipped_count == 1


# --- client: pagination + courtesy + block signal ------------------------ #
def _paged_client(
    *, pages: dict[int, dict], slept: list[float] | None = None, block_on: int | None = None
) -> DiaClient:
    slept = slept if slept is not None else []

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("page", "1"))
        if block_on is not None and page == block_on:
            return httpx.Response(429, json={"error": "slow down"})
        return httpx.Response(200, json=pages.get(page, {"search_items": [], "pagination": {}}))

    return DiaClient(
        user_agent=_UA,
        contact_email="ops@example.test",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=slept.append,
        delay=lambda lo, hi: 0.7,  # deterministic inside the [lo, hi] window
    )


def test_search_walks_all_pages_and_paces_requests() -> None:
    pages = {
        1: {"search_items": [_item(sku_id="A")], "pagination": {"total_pages": 2}},
        2: {"search_items": [_item(sku_id="B")], "pagination": {"total_pages": 2}},
    }
    slept: list[float] = []
    records = _paged_client(pages=pages, slept=slept).search("leche")
    assert [r["sku_id"] for r in records] == ["A", "B"]  # both pages collected in order
    assert slept == [0.7, 0.7]  # one courtesy delay before each page GET
    assert all(0.5 <= s <= 1.5 for s in slept)


def test_search_respects_max_products() -> None:
    pages = {
        1: {
            "search_items": [_item(sku_id="A"), _item(sku_id="B")],
            "pagination": {"total_pages": 5},
        },
    }
    records = _paged_client(pages=pages).search("leche", max_products=1)
    assert [r["sku_id"] for r in records] == ["A"]


def test_block_signal_stops_and_raises() -> None:
    with pytest.raises(ProviderResponseError):
        _paged_client(pages={}, block_on=1).search("leche")


# --- provider: gating + capabilities + metadata -------------------------- #
def test_provider_disabled_by_default_raises_on_iterate() -> None:
    provider = DiaProvider(settings=Settings())  # flag OFF by default
    assert provider.health_check().ok is False
    with pytest.raises(NotSupportedError):
        list(provider.iterate_products(ProductQuery()))


def test_provider_enabled_is_configured() -> None:
    provider = DiaProvider(settings=Settings(dia_connector_enabled=True))
    assert provider.health_check().ok is True


def test_provider_iterates_via_injected_client() -> None:
    pages = {1: {"search_items": [_item()], "pagination": {"total_pages": 1}}}
    provider = DiaProvider(client=_paged_client(pages=pages))
    got = list(provider.iterate_products(ProductQuery(search="leche")))
    assert [p.external_product_id for p in got] == ["504P6"]
    assert all(p.provider == "parsebot-dia" for p in got)  # LIVE provider code preserved


def test_capabilities_are_search_not_full_catalog() -> None:
    caps = DiaProvider(settings=Settings()).capabilities()
    assert caps.search is True
    assert caps.full_catalog is False and caps.store_scope is False


def test_metadata_is_honest_non_official() -> None:
    meta = DiaProvider(settings=Settings()).get_source_metadata()
    assert meta.provider_code == "parsebot-dia"
    assert meta.retailer_slug == "dia"
    assert meta.official is False


# --- monthly cadence ----------------------------------------------------- #
def test_dia_scheduler_cadence_is_monthly() -> None:
    cfg = SchedulerConfig().for_retailer("dia")
    assert cfg.cadence_for(RunType.CATALOG) == 30
    assert cfg.cadence_for(RunType.PRICES) == 30
