"""Tests for the NutriPlan PRICES read endpoints (FASE B §19).

Current-price freshness (fresh/stale/expired), honest store coverage + catalog-status,
product search and per-variant price history. All data is seeded directly into the
transactional session; no network is touched.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from cestaplan_api.models import (
    CoverageSnapshot,
    CrawlRun,
    ExternalProduct,
    PriceObservation,
    Product,
    ProductVariant,
    Retailer,
    Store,
)
from tests.api.conftest import login, register


@pytest.fixture()
def env(db_session: Session) -> dict:
    retailer = Retailer(
        slug="px-retailer", name="PX Retailer", adapter_key="test", is_synthetic=True
    )
    db_session.add(retailer)
    db_session.flush()
    store = Store(retailer_id=retailer.id, name="PX Store", is_synthetic=True)
    product = Product(name="Leche PX", is_synthetic=True)
    db_session.add_all([store, product])
    db_session.flush()
    external = ExternalProduct(retailer_id=retailer.id, external_id="PX-1")
    db_session.add(external)
    db_session.flush()
    variant = ProductVariant(
        product_id=product.id,
        retailer_id=retailer.id,
        external_product_id=external.id,
        display_name="Leche PX 1L",
        package_quantity=Decimal("1"),
        package_unit="l",
    )
    db_session.add(variant)
    db_session.flush()
    return {
        "db": db_session,
        "retailer": retailer,
        "store": store,
        "product": product,
        "variant": variant,
    }


def _auth(client: TestClient, email: str) -> None:
    register(client, email)
    login(client, email)


def _observe(env: dict, *, hours_ago: float, amount: str = "1.00") -> None:
    observed = datetime.now(UTC) - timedelta(hours=hours_ago)
    env["db"].add(
        PriceObservation(
            retailer_id=env["retailer"].id,
            store_id=env["store"].id,
            product_variant_id=env["variant"].id,
            price_scope="exact_store",
            price_type="regular",
            amount=Decimal(amount),
            currency="EUR",
            observed_at=observed,
            imported_at=observed,
            valid_from=observed,
            confidence_score=Decimal("0.9"),
        )
    )
    env["db"].flush()


@pytest.mark.parametrize(
    ("hours_ago", "expected"),
    [(1, "fresh"), (30, "stale"), (60, "expired")],
)
def test_current_price_freshness(
    client: TestClient, env: dict, hours_ago: float, expected: str
) -> None:
    _observe(env, hours_ago=hours_ago)
    _auth(client, f"px-fresh-{hours_ago}@example.com")
    resp = client.get(
        "/api/v1/prices/current",
        params={
            "variant_id": str(env["variant"].public_id),
            "store_id": str(env["store"].public_id),
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["freshness"] == expected
    assert body["amount"] == "1"  # string money (minimal fixed-point)
    assert body["price_scope"] == "exact_store"


def test_current_price_404_without_observation(client: TestClient, env: dict) -> None:
    _auth(client, "px-none@example.com")
    resp = client.get(
        "/api/v1/prices/current",
        params={"variant_id": str(env["variant"].public_id)},
    )
    assert resp.status_code == 404


def test_store_coverage_is_honest(client: TestClient, env: dict) -> None:
    env["db"].add(
        CoverageSnapshot(
            retailer_id=env["retailer"].id,
            store_id=env["store"].id,
            observed_at=datetime.now(UTC) - timedelta(hours=2),
            expected_products=10,
            discovered_products=10,
            priced_products=6,
            fresh_prices=5,
            stale_prices=1,
            estimated_prices=2,
            unavailable_products=0,
            coverage_ratio=Decimal("0.6000"),
            weighted_coverage_ratio=Decimal("0.8000"),
            status="partial",
        )
    )
    env["db"].flush()
    _auth(client, "px-cov@example.com")
    resp = client.get(f"/api/v1/stores/{env['store'].public_id}/coverage")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["has_snapshot"] is True
    assert body["status"] == "partial"  # not dressed up as complete
    assert body["priced_products"] == 6
    assert body["coverage_ratio"] == "0.6"
    assert body["freshness"] == "fresh"


def test_store_coverage_none_when_no_snapshot(client: TestClient, env: dict) -> None:
    _auth(client, "px-cov-none@example.com")
    resp = client.get(f"/api/v1/stores/{env['store'].public_id}/coverage")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["has_snapshot"] is False
    assert body["status"] == "none"
    assert body["priced_products"] == 0


def test_catalog_status_reports_counts_and_last_run(
    client: TestClient, env: dict
) -> None:
    env["db"].add(
        CoverageSnapshot(
            retailer_id=env["retailer"].id,
            store_id=env["store"].id,
            observed_at=datetime.now(UTC) - timedelta(hours=1),
            expected_products=8,
            discovered_products=8,
            priced_products=4,
            fresh_prices=3,
            stale_prices=1,
            estimated_prices=1,
            unavailable_products=0,
            coverage_ratio=Decimal("0.5000"),
            weighted_coverage_ratio=Decimal("0.7000"),
            status="partial",
        )
    )
    env["db"].add(
        CrawlRun(
            retailer_id=env["retailer"].id,
            store_id=env["store"].id,
            run_type="prices",
            status="completed",
            scheduled_at=datetime.now(UTC) - timedelta(hours=3),
            completed_at=datetime.now(UTC) - timedelta(hours=2),
            discovered_count=8,
            accepted_count=4,
        )
    )
    env["db"].flush()
    _auth(client, "px-catalog@example.com")
    resp = client.get(f"/api/v1/stores/{env['store'].public_id}/catalog-status")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["discovered_products"] == 8
    assert body["priced_products"] == 4
    assert body["fresh_prices"] == 3
    assert body["stale_prices"] == 1
    assert body["coverage_status"] == "partial"
    assert body["last_run"] is not None
    assert body["last_run"]["run_type"] == "prices"
    assert body["last_run"]["status"] == "completed"
    assert body["last_run"]["age_seconds"] is not None


def test_product_search_finds_variant(client: TestClient, env: dict) -> None:
    _auth(client, "px-search@example.com")
    resp = client.get("/api/v1/products/search", params={"q": "Leche PX"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["count"] >= 1
    ids = {item["variant_id"] for item in body["items"]}
    assert str(env["variant"].public_id) in ids


def test_variant_price_history(client: TestClient, env: dict) -> None:
    _observe(env, hours_ago=5, amount="0.90")
    _observe(env, hours_ago=1, amount="1.10")
    _auth(client, "px-hist@example.com")
    resp = client.get(f"/api/v1/products/{env['variant'].public_id}/prices")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["current"] is not None
    assert body["current"]["amount"] == "1.1"  # latest
    assert len(body["history"]) == 2
    # History ordered newest first, carries scope/type/date/age.
    first = body["history"][0]
    assert first["price_scope"] == "exact_store"
    assert first["price_type"] == "regular"
    assert "observed_at" in first
    assert "age_seconds" in first


def test_variant_prices_404_unknown_variant(client: TestClient, env: dict) -> None:
    import uuid

    _auth(client, "px-hist-404@example.com")
    resp = client.get(f"/api/v1/products/{uuid.uuid4()}/prices")
    assert resp.status_code == 404
