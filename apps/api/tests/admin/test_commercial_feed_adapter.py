"""CommercialFeedAdapter tests — HTTPX fully mocked, NO network / NO real provider calls.

Covers: mapping a provider payload -> NormalizedRecords via a given field map (Decimal money,
barcode, unit_price, promo -> promotion note); items-wrapper extraction; graceful degradation on
404 / network error / malformed payload; and the disabled-by-default gates (unconfigured adapter
refuses; registry reports enabled=False).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import httpx

from cestaplan_api.adapters.commercial_feed import (
    CF_ADAPTER_KEY,
    CommercialFeedAdapter,
    CommercialFeedConfig,
)
from cestaplan_api.adapters.registry import list_adapters

# A Pepesto-like unified JSON payload wrapped under "products".
_PAYLOAD = {
    "products": [
        {
            "ean": "8410000000001",
            "name": "Leche entera 1L",
            "brand": "MarcaX",
            "price": 0.95,
            "unit_price": 0.95,
            "promo_price": None,
            "category": "lacteos",
            "image": "https://cdn.example/leche.jpg",
        },
        {
            "ean": "8410000000002",
            "name": "Manzanas 1kg",
            "price": "1.80",
            "unit_price": "1.80",
            "promo_price": "1.50",  # active promo -> promotion note
            "category": "fruta",
        },
        {
            "ean": None,  # no identity at all -> skipped
            "name": "Suelto",
            "price": 2.0,
        },
        {
            "ean": "8410000000003",
            "name": "Sin precio",
            "price": None,  # no usable price -> skipped
        },
    ]
}

_MAP = {
    "barcode": "ean",
    "product_name": "name",
    "brand": "brand",
    "amount": "price",
    "unit_price": "unit_price",
    "promo_price": "promo_price",
    "category": "category",
}


def _config(
    *,
    field_map: dict[str, str] | None = None,
    items_path: str = "products",
    auth_header: str = "Authorization: Bearer",
) -> CommercialFeedConfig:
    return CommercialFeedConfig(
        base_url="https://feed.example.com",
        api_key="secret-key",
        auth_header=auth_header,
        products_path="/v1/products",
        pagination="none",
        items_path=items_path,
        field_map=dict(_MAP) if field_map is None else field_map,
        source_name="Feed comercial autorizado",
        attribution="Precios cedidos por proveedor autorizado.",
        license_code="proprietary",
    )


def _adapter_with(handler, *, config: CommercialFeedConfig | None = None) -> CommercialFeedAdapter:
    transport = httpx.MockTransport(handler)
    return CommercialFeedAdapter(
        client=httpx.Client(transport=transport), config=config or _config()
    )


def _ok_handler(request: httpx.Request) -> httpx.Response:
    # The operator's key is sent via the configured auth header (Bearer here).
    assert request.headers["Authorization"] == "Bearer secret-key"
    assert request.headers["User-Agent"].startswith("CestaPlan/")
    assert "feed.example.com/v1/products" in str(request.url)
    return httpx.Response(200, json=_PAYLOAD)


_OBSERVED = datetime(2026, 7, 22, tzinfo=UTC)


def _fetch(adapter: CommercialFeedAdapter):
    return adapter.fetch_products(
        retailer_slug="feed-es",
        store_external_code="store:1",
        default_observed_at=_OBSERVED,
    )


def test_maps_payload_to_records_with_decimal_money() -> None:
    records = _fetch(_adapter_with(_ok_handler))
    # Two rows survive (no-identity and no-price rows are skipped, never fabricated).
    assert len(records) == 2
    by_bc = {r.barcode: r for r in records}

    milk = by_bc["8410000000001"]
    assert milk.product_name == "Leche entera 1L"
    assert milk.brand == "MarcaX"
    assert milk.amount == Decimal("0.95")
    assert isinstance(milk.amount, Decimal)
    assert milk.unit_price == Decimal("0.95")
    assert milk.currency == "EUR"
    assert milk.source_type == "authorized_partner"
    assert milk.source_name == "Feed comercial autorizado"
    assert milk.observed_at == _OBSERVED
    assert milk.category == "lacteos"
    assert milk.promotion is None
    assert milk.product_external_id == "8410000000001"


def test_promo_price_becomes_promotion_note() -> None:
    records = _fetch(_adapter_with(_ok_handler))
    apples = next(r for r in records if r.barcode == "8410000000002")
    assert apples.amount == Decimal("1.80")  # numeric string parsed to Decimal
    assert apples.promotion == "Precio promocionado 1.50"


def test_unconfigured_adapter_returns_empty() -> None:
    # No mapping -> not configured -> refuses without any HTTP call.
    cfg = _config(field_map={})
    assert cfg.configured is False
    called = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        called["n"] += 1
        return httpx.Response(200, json=_PAYLOAD)

    assert _fetch(_adapter_with(handler, config=cfg)) == []
    assert called["n"] == 0


def test_404_returns_empty() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "not found"})

    assert _fetch(_adapter_with(handler)) == []


def test_network_error_returns_empty() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    assert _fetch(_adapter_with(handler)) == []


def test_malformed_payload_returns_empty() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=b"not-json{", headers={"content-type": "application/json"}
        )

    assert _fetch(_adapter_with(handler)) == []


def test_bare_list_payload_supported() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_PAYLOAD["products"])

    records = _fetch(_adapter_with(handler, config=_config(items_path="")))
    assert {r.barcode for r in records} == {"8410000000001", "8410000000002"}


def test_x_api_key_auth_header() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-api-key"] == "secret-key"
        assert "Authorization" not in request.headers
        return httpx.Response(200, json=_PAYLOAD)

    records = _fetch(_adapter_with(handler, config=_config(auth_header="x-api-key")))
    assert len(records) == 2


def test_registry_reports_disabled_by_default() -> None:
    listing = next(a for a in list_adapters() if a.adapter_key == CF_ADAPTER_KEY)
    # With no env config the connector is unconfigured -> not enabled in the registry view.
    assert listing.enabled is False
    assert listing.source_type == "authorized_partner"
    assert listing.capabilities.supports_get_price is True
    assert listing.requires_network is True
