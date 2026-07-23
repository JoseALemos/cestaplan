"""OpenPricesProvider (FASE 5 piece) — offline, with a fake adapter.

Verifies the OpenPrice -> ExternalCatalogProduct mapping: barcode as external id, exact-store
scope, Decimal money, discount surfaced as a promotional price (never fabricated), by-weight
unit pricing, rows without a barcode skipped, the max_products bound, and never 'official'.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from cestaplan_api.adapters.openprices import OpenPrice
from cestaplan_api.ingestion.contracts import PriceScope
from cestaplan_api.ingestion.providers.contracts import ProductQuery, ProviderKind, SellUnit
from cestaplan_api.ingestion.providers.open_prices.provider import OpenPricesProvider


class _FakeAdapter:
    def __init__(self, prices: list[OpenPrice]) -> None:
        self._prices = prices
        self.calls: list[tuple[int, str]] = []

    def fetch_store_prices(self, osm_id: int, osm_type: str) -> list[OpenPrice]:
        self.calls.append((osm_id, osm_type))
        return self._prices

    def fetch_locations(self, country_code: str = "ES") -> list:
        return []


def _price(**kw) -> OpenPrice:
    base = {
        "price_id": 1,
        "amount": Decimal("0.88"),
        "currency": "EUR",
        "observed_on": date(2026, 7, 20),
        "source_url": "https://prices.openfoodfacts.org/prices/1",
        "barcode": "8410000000001",
        "product_name": "Leche",
    }
    base.update(kw)
    return OpenPrice(**base)


def _provider(prices: list[OpenPrice]) -> tuple[OpenPricesProvider, _FakeAdapter]:
    adapter = _FakeAdapter(prices)
    return OpenPricesProvider(adapter), adapter  # type: ignore[arg-type]


def test_metadata_is_community_not_official() -> None:
    meta = _provider([])[0].get_source_metadata()
    assert meta.official is False
    assert meta.kind is ProviderKind.COMMUNITY


def test_maps_price_to_exact_store_decimal() -> None:
    provider, adapter = _provider([_price()])
    products = list(provider.iterate_products(ProductQuery(store_external_id="osm:NODE/123456")))
    assert adapter.calls == [(123456, "NODE")]
    assert len(products) == 1
    p = products[0]
    assert p.external_product_id == "8410000000001"
    assert p.price_scope is PriceScope.EXACT_STORE
    assert isinstance(p.regular_price, Decimal) and p.regular_price == Decimal("0.88")
    assert p.promotional_price is None
    assert p.sell_unit is SellUnit.UNIT
    assert p.external_store_id == "osm:NODE/123456"


def test_discount_becomes_promotional_price() -> None:
    provider, _ = _provider(
        [
            _price(
                price_is_discounted=True,
                price_without_discount=Decimal("1.10"),
                amount=Decimal("0.88"),
            )
        ]
    )
    p = next(provider.iterate_products(ProductQuery(store_external_id="osm:NODE/1")))
    assert p.regular_price == Decimal("1.10")  # the undiscounted price
    assert p.promotional_price == Decimal("0.88")  # the observed (lower) price


def test_by_weight_sets_unit_price() -> None:
    provider, _ = _provider([_price(price_per="KILOGRAM", amount=Decimal("2.30"))])
    p = next(provider.iterate_products(ProductQuery(store_external_id="osm:WAY/9")))
    assert p.sell_unit is SellUnit.WEIGHT
    assert p.unit_price == Decimal("2.30")
    assert p.unit_price_unit == "kg"


def test_rows_without_barcode_are_skipped() -> None:
    provider, _ = _provider([_price(barcode=None), _price(barcode="8410000000002")])
    products = list(provider.iterate_products(ProductQuery(store_external_id="osm:NODE/1")))
    assert [p.external_product_id for p in products] == ["8410000000002"]


def test_max_products_bound() -> None:
    provider, _ = _provider([_price(price_id=i, barcode=f"84100000000{i:02d}") for i in range(5)])
    products = list(
        provider.iterate_products(ProductQuery(store_external_id="osm:NODE/1", max_products=2))
    )
    assert len(products) == 2


def test_no_store_scope_yields_nothing() -> None:
    provider, adapter = _provider([_price()])
    assert list(provider.iterate_products(ProductQuery())) == []
    assert adapter.calls == []  # never attempts a national pull
