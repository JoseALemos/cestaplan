"""OpenPricesAdapter tests — HTTPX fully mocked, NO network / NO real Open Prices calls.

Covers: parsing a paginated ``/prices`` payload, Decimal money, barcode/no-barcode rows,
discount fields, pagination, and graceful degradation on 404 / network error / malformed
payload (partial result, never a crash, never fabricated data).
"""

from __future__ import annotations

from decimal import Decimal

import httpx

from cestaplan_api.adapters.openprices import OpenPricesAdapter

# A realistic Open Prices ``/prices`` page. Item 2 has no ``product_code`` (a loose/category
# price the sync skips); item 3 is a discounted per-kilogram price.
_PAGE = {
    "items": [
        {
            "id": 101,
            "product_code": "8410000000001",
            "product_name": "Leche entera",
            "price": 0.95,
            "currency": "EUR",
            "date": "2026-04-10",
            "price_per": None,
            "price_is_discounted": False,
            "price_without_discount": None,
            "location_osm_id": 677280352,
            "location_osm_type": "WAY",
            "location": {"osm_id": 677280352, "osm_type": "WAY", "osm_name": "Lidl"},
        },
        {
            "id": 102,
            "product_code": None,  # category/loose item -> barcode None
            "product_name": "ESPARRAGO VERDE",
            "price": "3.89",
            "currency": "EUR",
            "date": "2026-04-10",
            "price_per": "UNIT",
        },
        {
            "id": 103,
            "product_code": "8410000000002",
            "product_name": "Manzanas",
            "price": 1.80,
            "currency": "EUR",
            "date": "2026-04-11",
            "price_per": "KILOGRAM",
            "price_is_discounted": True,
            "price_without_discount": 2.20,
        },
    ],
    "page": 1,
    "pages": 1,
    "size": 100,
    "total": 3,
}


def _adapter_with(handler) -> OpenPricesAdapter:
    transport = httpx.MockTransport(handler)
    return OpenPricesAdapter(client=httpx.Client(transport=transport))


def _ok_handler(request: httpx.Request) -> httpx.Response:
    assert "prices.openfoodfacts.org" in str(request.url)
    assert request.headers["User-Agent"].startswith("CestaPlan/")
    return httpx.Response(200, json=_PAGE)


def test_parses_prices_with_decimal_money() -> None:
    prices = _adapter_with(_ok_handler).fetch_store_prices(677280352, "WAY")
    assert len(prices) == 3
    by_id = {p.price_id: p for p in prices}

    milk = by_id[101]
    assert milk.barcode == "8410000000001"
    assert milk.product_name == "Leche entera"
    assert milk.amount == Decimal("0.95")
    assert isinstance(milk.amount, Decimal)
    assert milk.currency == "EUR"
    assert milk.observed_on.isoformat() == "2026-04-10"
    assert milk.source_url == "https://prices.openfoodfacts.org/prices/101"
    assert milk.price_per is None


def test_no_barcode_row_kept_with_none() -> None:
    prices = _adapter_with(_ok_handler).fetch_store_prices(677280352, "WAY")
    loose = next(p for p in prices if p.price_id == 102)
    assert loose.barcode is None
    assert loose.amount == Decimal("3.89")  # numeric string parsed to Decimal
    assert loose.price_per == "UNIT"


def test_discount_fields_parsed() -> None:
    prices = _adapter_with(_ok_handler).fetch_store_prices(677280352, "WAY")
    apples = next(p for p in prices if p.price_id == 103)
    assert apples.price_is_discounted is True
    assert apples.price_without_discount == Decimal("2.20")
    assert apples.price_per == "KILOGRAM"


def test_pagination_follows_pages() -> None:
    page1 = {**_PAGE, "page": 1, "pages": 2}
    page2 = {
        "items": [
            {
                "id": 201,
                "product_code": "8410000000003",
                "product_name": "Pan",
                "price": 1.10,
                "currency": "EUR",
                "date": "2026-04-12",
            }
        ],
        "page": 2,
        "pages": 2,
        "size": 100,
        "total": 4,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        page = request.url.params.get("page")
        return httpx.Response(200, json=page2 if page == "2" else page1)

    prices = _adapter_with(handler).fetch_store_prices(677280352, "WAY")
    assert len(prices) == 4
    assert any(p.price_id == 201 for p in prices)


def test_404_returns_empty() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "not found"})

    assert _adapter_with(handler).fetch_store_prices(1, "WAY") == []


def test_network_error_returns_empty() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    assert _adapter_with(handler).fetch_store_prices(1, "WAY") == []


def test_timeout_returns_empty() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    assert _adapter_with(handler).fetch_store_prices(1, "WAY") == []


def test_malformed_payload_returns_empty() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=b"not-json{", headers={"content-type": "application/json"}
        )

    assert _adapter_with(handler).fetch_store_prices(1, "WAY") == []


def test_row_without_price_or_date_is_skipped() -> None:
    payload = {
        "items": [
            {"id": 1, "product_code": "x", "price": None, "date": "2026-04-10"},
            {"id": 2, "product_code": "y", "price": 1.0, "date": ""},
            {"id": 3, "product_code": "z", "price": 2.0, "date": "2026-04-10"},
        ],
        "page": 1,
        "pages": 1,
    }

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    prices = _adapter_with(handler).fetch_store_prices(1, "WAY")
    assert [p.price_id for p in prices] == [3]  # only the usable row survives
