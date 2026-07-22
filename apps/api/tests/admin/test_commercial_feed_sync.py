"""Commercial-feed sync service + admin endpoint tests — HTTPX mocked, NO network.

Covers: disabled-by-default (registry off, sync no-op, admin 409); a configured+enabled run
upserting real Product + ProductBarcode + ProductPrice tagged ``authorized_partner`` with Decimal
money; idempotency (run twice -> no duplicate observations); optional OFF enrichment; and admin
authz (non-admin 403 / missing CSRF 403).
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

from cestaplan_api.adapters.commercial_feed import CommercialFeedAdapter, CommercialFeedConfig
from cestaplan_api.config import Settings
from cestaplan_api.models import (
    Product,
    ProductBarcode,
    ProductPrice,
    Retailer,
    Store,
)
from cestaplan_api.services import commercial_feed_sync, enrichment

from .conftest import csrf, login, promote_to_admin, register

_MAP = {
    "barcode": "ean",
    "product_name": "name",
    "brand": "brand",
    "amount": "price",
    "unit_price": "unit_price",
    "promo_price": "promo_price",
}

_PAYLOAD = {
    "products": [
        {
            "ean": "8410000000001",
            "name": "Leche entera 1L",
            "brand": "MarcaX",
            "price": 0.95,
            "unit_price": 0.95,
        },
        {
            "ean": "8410000000002",
            "name": "Manzanas 1kg",
            "price": "1.80",
            "unit_price": "1.80",
            "promo_price": "1.50",
        },
    ]
}


def _email() -> str:
    return f"admin-{uuid.uuid4().hex[:12]}@example.com"


def _settings() -> Settings:
    return Settings(
        commercial_feed_base_url="https://feed.example.com",
        commercial_feed_api_key="secret-key",
        commercial_feed_products_path="/v1/products",
        commercial_feed_items_path="products",
        commercial_feed_mapping=json.dumps(_MAP),
    )


def _adapter(payload: dict | None = None, *, status_code: int = 200) -> CommercialFeedAdapter:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json=payload if payload is not None else _PAYLOAD)

    config = CommercialFeedConfig.from_settings(_settings())
    return CommercialFeedAdapter(
        client=httpx.Client(transport=httpx.MockTransport(handler)), config=config
    )


def _isolate_stores(db: Session) -> None:
    existing = db.execute(
        select(Store)
        .join(Retailer, Retailer.id == Store.retailer_id)
        .where(Retailer.adapter_key == "commercial_feed", Store.is_active.is_(True))
    ).scalars().all()
    for st in existing:
        st.is_active = False
    db.flush()


def _make_store(db: Session, *, name: str = "SuperFeed") -> Store:
    retailer = Retailer(
        slug=f"feed-{uuid.uuid4().hex[:8]}",
        name=name,
        adapter_key="commercial_feed",
        country="ES",
        is_active=True,
        is_synthetic=False,
    )
    db.add(retailer)
    db.flush()
    store = Store(
        retailer_id=retailer.id,
        external_code=f"feed-store-{uuid.uuid4().hex[:8]}",
        name=name,
        is_active=True,
        is_synthetic=False,
    )
    db.add(store)
    db.flush()
    return store


def _enable_source(db: Session) -> None:
    ds = commercial_feed_sync.ensure_commercial_feed_data_source(db, _settings())
    ds.is_enabled = True
    db.flush()


def _disable_off(db: Session) -> None:
    off = enrichment.ensure_off_data_source(db)
    off.is_enabled = False
    db.flush()


# --------------------------------------------------------------------------- #
# Disabled by default
# --------------------------------------------------------------------------- #
def test_disabled_by_default(db_session: Session) -> None:
    # The ensured DataSource row is created disabled; and default settings are unconfigured.
    ds = commercial_feed_sync.ensure_commercial_feed_data_source(db_session)
    assert ds.is_enabled is False
    assert commercial_feed_sync.commercial_feed_enabled(db_session) is False


def test_sync_all_no_op_when_disabled(db_session: Session) -> None:
    _isolate_stores(db_session)
    store = _make_store(db_session)
    result = commercial_feed_sync.sync_all(db_session, settings=_settings())
    # Configured (settings) but the source row is still disabled -> nothing runs.
    assert result.configured is True
    assert result.enabled is False
    assert result.stores_synced == 0
    prices = db_session.execute(
        select(func.count(ProductPrice.id)).where(ProductPrice.store_id == store.id)
    ).scalar_one()
    assert prices == 0


# --------------------------------------------------------------------------- #
# Configured + enabled: real upsert, authorized_partner, Decimal, idempotent
# --------------------------------------------------------------------------- #
def test_sync_upserts_authorized_partner_prices(db_session: Session) -> None:
    store = _make_store(db_session)
    summary = commercial_feed_sync.sync_commercial_feed(
        db_session, store, adapter=_adapter(), settings=_settings()
    )
    assert summary.inserted == 2
    assert summary.products_created == 2
    assert summary.barcodes_created == 2

    prices = db_session.execute(
        select(ProductPrice).where(ProductPrice.store_id == store.id)
    ).scalars().all()
    assert len(prices) == 2
    assert all(p.source_type == "authorized_partner" for p in prices)
    assert all(p.is_synthetic is False for p in prices)
    assert all(isinstance(p.amount, Decimal) for p in prices)
    assert {p.amount for p in prices} == {Decimal("0.95"), Decimal("1.80")}

    apples = next(p for p in prices if p.amount == Decimal("1.80"))
    assert apples.promotion == "Precio promocionado 1.50"

    product = db_session.execute(
        select(Product).where(Product.external_id == "8410000000001")
    ).scalar_one()
    assert product.is_synthetic is False
    assert product.brand is None  # brand not mapped into Product by the price sync itself
    bc = db_session.execute(
        select(ProductBarcode).where(ProductBarcode.product_id == product.id)
    ).scalars().all()
    assert [b.barcode for b in bc] == ["8410000000001"]


def test_sync_is_idempotent(db_session: Session) -> None:
    store = _make_store(db_session)
    first = commercial_feed_sync.sync_commercial_feed(
        db_session, store, adapter=_adapter(), settings=_settings()
    )
    second = commercial_feed_sync.sync_commercial_feed(
        db_session, store, adapter=_adapter(), settings=_settings()
    )
    assert first.inserted == 2
    assert second.inserted == 0
    assert second.skipped_existing == 2
    total = db_session.execute(
        select(func.count(ProductPrice.id)).where(ProductPrice.store_id == store.id)
    ).scalar_one()
    assert total == 2  # append-only, no duplicates


def test_off_enrich_optional_disabled_keeps_prices(db_session: Session) -> None:
    store = _make_store(db_session)
    _disable_off(db_session)
    summary = commercial_feed_sync.sync_and_enrich_store(
        db_session, store, adapter=_adapter(), settings=_settings(), enrich=True
    )
    assert summary.inserted == 2
    assert summary.products_enriched == 0  # OFF disabled -> no enrichment, prices kept


# --------------------------------------------------------------------------- #
# Admin endpoint: authz + csrf + disabled 409 + happy path
# --------------------------------------------------------------------------- #
def test_admin_sync_requires_admin(client: TestClient) -> None:
    email = _email()
    register(client, email)
    token = login(client, email)
    resp = client.post(
        "/api/v1/admin/sources/commercial-feed/sync", json={}, headers=csrf(token)
    )
    assert resp.status_code == 403


def test_admin_sync_requires_csrf(client: TestClient, db_session: Session) -> None:
    email = _email()
    register(client, email)
    login(client, email)
    promote_to_admin(db_session, email)
    resp = client.post("/api/v1/admin/sources/commercial-feed/sync", json={})
    assert resp.status_code == 403


def test_admin_sync_409_when_disabled(client: TestClient, db_session: Session) -> None:
    email = _email()
    register(client, email)
    token = login(client, email)
    promote_to_admin(db_session, email)
    resp = client.post(
        "/api/v1/admin/sources/commercial-feed/sync", json={}, headers=csrf(token)
    )
    assert resp.status_code == 409


def test_admin_sync_runs_when_configured_and_enabled(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(commercial_feed_sync, "get_settings", _settings)
    monkeypatch.setattr(
        commercial_feed_sync, "CommercialFeedAdapter", lambda: _adapter()
    )
    email = _email()
    register(client, email)
    token = login(client, email)
    promote_to_admin(db_session, email)
    _isolate_stores(db_session)
    _make_store(db_session, name="SuperFeed")
    _enable_source(db_session)
    _disable_off(db_session)  # avoid a live OFF call during enrichment

    resp = client.post(
        "/api/v1/admin/sources/commercial-feed/sync", json={}, headers=csrf(token)
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["stores_synced"] == 1
    assert body["inserted"] == 2
    assert body["license_code"] == "proprietary"
    assert all(r["fetched"] == 2 for r in body["results"])
