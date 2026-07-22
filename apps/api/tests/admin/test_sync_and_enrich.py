"""sync_and_enrich + orchestration + ``/sources/sync-all`` tests — HTTPX mocked, NO network.

Covers: a real Open-Prices product getting BOTH a price and OFF nutrition/allergens/brand;
graceful OFF 404 (price kept, product un-enriched, no crash); prices are NEVER taken from OFF;
the ``sync_all_and_enrich`` combined per-chain summary; disabled-source skipping (Open Prices
disabled → nothing synced; OFF disabled → prices kept, no enrichment); and the admin endpoint
authz (non-admin 403 / missing-CSRF 403).
"""

from __future__ import annotations

import json
import uuid
from decimal import Decimal

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from cestaplan_api.adapters.openfoodfacts import OpenFoodFactsAdapter
from cestaplan_api.adapters.openprices import OpenPricesAdapter
from cestaplan_api.models import (
    Product,
    ProductNutrition,
    ProductPrice,
    Retailer,
    Store,
)
from cestaplan_api.services import enrichment, open_prices_sync

from .conftest import csrf, login, promote_to_admin, register

_OSM_ID = 677280352
_OSM_TYPE = "WAY"

_OP_PAGE = {
    "items": [
        {
            "id": 101,
            "product_code": "8410000000001",
            "product_name": "Leche entera",
            "price": 0.95,
            "currency": "EUR",
            "date": "2026-04-10",
        },
        {
            "id": 103,
            "product_code": "8410000000002",
            "product_name": "Manzanas",
            "price": 1.80,
            "currency": "EUR",
            "date": "2026-04-11",
            "price_per": "KILOGRAM",
        },
    ],
    "page": 1,
    "pages": 1,
}

_OFF_PAYLOAD = {
    "status": 1,
    "product": {
        "product_name": "Leche entera",
        "brands": "MarcaX",
        "categories_tags": ["en:dairies", "en:milks"],
        "allergens_tags": ["en:milk"],
        "nutriments": {
            "energy-kcal_100g": 64,
            "proteins_100g": 3.1,
            "carbohydrates_100g": 4.7,
            "fat_100g": 3.6,
            "fiber_100g": 0,
        },
        "image_url": "https://images.openfoodfacts.org/leche.jpg",
        "price": "1.23",  # planted noise; must never be read/stored as a price
    },
}


def _email() -> str:
    return f"admin-{uuid.uuid4().hex[:12]}@example.com"


def _op_adapter(payload: dict | None = None) -> OpenPricesAdapter:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload if payload is not None else _OP_PAGE)

    return OpenPricesAdapter(client=httpx.Client(transport=httpx.MockTransport(handler)))


def _off_adapter(*, status_code: int = 200) -> OpenFoodFactsAdapter:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json=_OFF_PAYLOAD)

    return OpenFoodFactsAdapter(client=httpx.Client(transport=httpx.MockTransport(handler)))


def _isolate_op_stores(db: Session) -> None:
    """Deactivate any pre-existing active Open-Prices stores so ``open_prices_stores`` returns
    only the store this test creates (the shared DB may already hold seeded rows; the change is
    rolled back with the test transaction)."""
    existing = db.execute(
        select(Store)
        .join(Retailer, Retailer.id == Store.retailer_id)
        .where(Retailer.adapter_key == "open_prices", Store.is_active.is_(True))
    ).scalars().all()
    for st in existing:
        st.is_active = False
    db.flush()


def _make_store(db: Session, *, name: str = "Lidl") -> Store:
    retailer = Retailer(
        slug=f"lidl-{uuid.uuid4().hex[:8]}",
        name=name,
        adapter_key="open_prices",
        country="ES",
        is_active=True,
        is_synthetic=False,
    )
    db.add(retailer)
    db.flush()
    store = Store(
        retailer_id=retailer.id,
        external_code=open_prices_sync.store_external_code(_OSM_TYPE, _OSM_ID),
        name=name,
        is_active=True,
        is_synthetic=False,
    )
    db.add(store)
    db.flush()
    return store


# --------------------------------------------------------------------------- #
# sync_and_enrich_store: price + OFF data; prices never from OFF
# --------------------------------------------------------------------------- #
def test_sync_and_enrich_writes_price_and_nutrition(db_session: Session) -> None:
    store = _make_store(db_session)
    summary = open_prices_sync.sync_and_enrich_store(
        db_session, store, op_adapter=_op_adapter(), off_adapter=_off_adapter()
    )

    assert summary.inserted == 2  # real prices from Open Prices
    assert summary.products_enriched == 2  # both products enriched from OFF

    prices = db_session.execute(
        select(ProductPrice).where(ProductPrice.store_id == store.id)
    ).scalars().all()
    assert len(prices) == 2
    assert all(p.source_type == "open_dataset" for p in prices)

    nutr = db_session.execute(
        select(ProductNutrition)
        .join(Product, Product.id == ProductNutrition.product_id)
        .where(Product.external_id == "8410000000001")
    ).scalar_one()
    assert nutr.source_type == "open_dataset"
    assert set(nutr.allergens or []) == {"milk"}
    assert nutr.energy_kcal is not None

    product = db_session.execute(
        select(Product).where(Product.external_id == "8410000000001")
    ).scalar_one()
    assert product.brand == "MarcaX"
    assert product.image_url == "https://images.openfoodfacts.org/leche.jpg"

    # The OFF-planted "price" was never read: prices come only from Open Prices.
    amounts = {p.amount for p in prices}
    assert Decimal("1.23") not in amounts
    assert amounts == {Decimal("0.95"), Decimal("1.80")}


def test_sync_and_enrich_off_404_keeps_price_unenriched(db_session: Session) -> None:
    store = _make_store(db_session)
    summary = open_prices_sync.sync_and_enrich_store(
        db_session, store, op_adapter=_op_adapter(), off_adapter=_off_adapter(status_code=404)
    )

    assert summary.inserted == 2  # prices kept
    assert summary.products_enriched == 0  # OFF miss -> nothing enriched, no crash
    # None of this store's just-created products got a nutrition row.
    nut_count = db_session.execute(
        select(func.count(ProductNutrition.id))
        .join(Product, Product.id == ProductNutrition.product_id)
        .where(Product.retailer_id == store.retailer_id)
    ).scalar_one()
    assert nut_count == 0


def test_sync_and_enrich_off_disabled_keeps_price(db_session: Session) -> None:
    store = _make_store(db_session)
    ds = enrichment.ensure_off_data_source(db_session)
    ds.is_enabled = False
    db_session.flush()

    summary = open_prices_sync.sync_and_enrich_store(
        db_session, store, op_adapter=_op_adapter(), off_adapter=_off_adapter()
    )
    assert summary.inserted == 2
    assert summary.products_enriched == 0


# --------------------------------------------------------------------------- #
# Orchestration: combined per-chain summary + disabled-source skipping
# --------------------------------------------------------------------------- #
def test_sync_all_and_enrich_summary(db_session: Session) -> None:
    _isolate_op_stores(db_session)
    store = _make_store(db_session, name="Mercadona")
    result = open_prices_sync.sync_all_and_enrich(
        db_session, op_adapter=_op_adapter(), off_adapter=_off_adapter()
    )
    assert result.open_prices_enabled is True
    assert result.stores_synced == 1
    assert result.prices_inserted == 2
    assert result.products_enriched == 2
    assert result.per_chain["Mercadona"]["stores"] == 1
    assert result.per_chain["Mercadona"]["prices_inserted"] == 2
    assert result.per_chain["Mercadona"]["products_enriched"] == 2
    assert store.public_id is not None


def test_sync_all_open_prices_disabled_skips(db_session: Session) -> None:
    _isolate_op_stores(db_session)
    store = _make_store(db_session)
    ds = open_prices_sync.ensure_open_prices_data_source(db_session)
    ds.is_enabled = False
    db_session.flush()

    result = open_prices_sync.sync_all_and_enrich(
        db_session, op_adapter=_op_adapter(), off_adapter=_off_adapter()
    )
    assert result.open_prices_enabled is False
    assert result.stores_synced == 0
    assert result.prices_inserted == 0
    # Nothing was synced for our store either.
    my_prices = db_session.execute(
        select(func.count(ProductPrice.id)).where(ProductPrice.store_id == store.id)
    ).scalar_one()
    assert my_prices == 0


def test_sync_all_off_disabled_keeps_prices_no_enrich(db_session: Session) -> None:
    _isolate_op_stores(db_session)
    _make_store(db_session)
    ds = enrichment.ensure_off_data_source(db_session)
    ds.is_enabled = False
    db_session.flush()

    result = open_prices_sync.sync_all_and_enrich(
        db_session, op_adapter=_op_adapter(), off_adapter=_off_adapter()
    )
    assert result.openfoodfacts_enabled is False
    assert result.prices_inserted == 2
    assert result.products_enriched == 0


# --------------------------------------------------------------------------- #
# Admin endpoint: authz + csrf + happy path
# --------------------------------------------------------------------------- #
def test_admin_sync_all_requires_admin(client: TestClient) -> None:
    email = _email()
    register(client, email)
    token = login(client, email)
    resp = client.post("/api/v1/admin/sources/sync-all", json={}, headers=csrf(token))
    assert resp.status_code == 403


def test_admin_sync_all_requires_csrf(client: TestClient, db_session: Session) -> None:
    email = _email()
    register(client, email)
    login(client, email)
    promote_to_admin(db_session, email)
    resp = client.post("/api/v1/admin/sources/sync-all", json={})
    assert resp.status_code == 403


def test_admin_sync_all_runs(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(open_prices_sync, "OpenPricesAdapter", lambda: _op_adapter())
    monkeypatch.setattr(open_prices_sync, "OpenFoodFactsAdapter", lambda: _off_adapter())
    email = _email()
    register(client, email)
    token = login(client, email)
    promote_to_admin(db_session, email)
    _isolate_op_stores(db_session)
    _make_store(db_session, name="Carrefour")

    resp = client.post("/api/v1/admin/sources/sync-all", json={}, headers=csrf(token))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["stores_synced"] == 1
    assert body["prices_inserted"] == 2
    assert body["products_enriched"] == 2
    assert body["per_chain"]["Carrefour"]["prices_inserted"] == 2
    assert "ODbL" in body["license_code"]
    # No OFF-planted price leaks into the response.
    assert "1.23" not in json.dumps(body)
