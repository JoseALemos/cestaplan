"""Tests for ``POST /prices/resolve-basket`` (FASE B §19).

Whole-package math, promotion application (2x1), honest unresolved items and the
known-vs-estimated total split. Observations are seeded directly into the transactional
session; no network is ever touched. Money is asserted as strings on the wire.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from cestaplan_api.models import (
    ExternalProduct,
    PriceObservation,
    Product,
    ProductVariant,
    PromotionRule,
    Retailer,
    Store,
)
from tests.api.conftest import login, register


@pytest.fixture()
def basket_env(db_session: Session) -> dict:
    """A retailer/store/product/variant with a fresh 1.20 EUR / 500 g observation."""
    retailer = Retailer(
        slug="rb-retailer", name="RB Retailer", adapter_key="test", is_synthetic=True
    )
    db_session.add(retailer)
    db_session.flush()

    store = Store(retailer_id=retailer.id, name="RB Store", is_synthetic=True)
    product = Product(name="Arroz RB", is_synthetic=True)
    db_session.add_all([store, product])
    db_session.flush()

    external = ExternalProduct(retailer_id=retailer.id, external_id="RB-1")
    db_session.add(external)
    db_session.flush()

    variant = ProductVariant(
        product_id=product.id,
        retailer_id=retailer.id,
        external_product_id=external.id,
        display_name="Arroz RB 500g",
        package_quantity=Decimal("500"),
        package_unit="g",
    )
    db_session.add(variant)
    db_session.flush()

    observed = datetime.now(UTC) - timedelta(hours=1)
    obs = PriceObservation(
        retailer_id=retailer.id,
        store_id=store.id,
        product_variant_id=variant.id,
        price_scope="exact_store",
        price_type="regular",
        amount=Decimal("1.20"),
        currency="EUR",
        observed_at=observed,
        imported_at=observed,
        valid_from=observed,
        confidence_score=Decimal("0.95"),
    )
    db_session.add(obs)
    db_session.flush()

    return {
        "db": db_session,
        "retailer": retailer,
        "store": store,
        "product": product,
        "variant": variant,
        "observation": obs,
    }


def _auth(client: TestClient, email: str) -> None:
    register(client, email)
    login(client, email)


def test_whole_package_math_and_string_money(
    client: TestClient, basket_env: dict
) -> None:
    _auth(client, "rb-basic@example.com")
    resp = client.post(
        "/api/v1/prices/resolve-basket",
        json={
            "store_id": str(basket_env["store"].public_id),
            "target_date": "2026-07-25",
            "items": [
                {
                    "variant_id": str(basket_env["variant"].public_id),
                    "required_quantity": "600",
                    "unit": "g",
                }
            ],
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["lines"]) == 1
    line = body["lines"][0]
    # 600 g needed, 500 g packs -> 2 packs, 1000 g bought, 600 used, 400 leftover.
    assert line["packages"] == 2
    assert line["purchased_quantity"] == "1000"
    assert line["used_quantity"] == "600"
    assert line["leftover"] == "400"
    assert line["unit_price"] == "1.2"
    assert line["line_cost"] == "2.4"  # 2 packs, no promo
    assert line["promotion_applied"] is False
    # Provenance / freshness present and honest.
    assert line["freshness"] == "fresh"
    assert line["price_scope"] == "exact_store"
    assert isinstance(line["age_seconds"], str)
    assert line["confidence"] == "0.95"
    # Money is a string, not a float.
    assert isinstance(line["line_cost"], str)
    assert body["totals"]["known_cost"] == "2.4"
    assert body["totals"]["estimated_cost"] == "0"
    assert body["coverage"]["coverage_ratio"] == "1"
    assert body["target_date"] == "2026-07-25"


def test_two_for_one_promotion_charges_ceil_half(
    client: TestClient, basket_env: dict
) -> None:
    db: Session = basket_env["db"]
    # Attach a 2x1 promotion to the observation.
    db.add(
        PromotionRule(
            price_observation_id=basket_env["observation"].id,
            type="nxm",
            required_quantity=2,
            charged_quantity=1,
            raw_text="2x1",
        )
    )
    db.flush()

    _auth(client, "rb-promo@example.com")
    resp = client.post(
        "/api/v1/prices/resolve-basket",
        json={
            "store_id": str(basket_env["store"].public_id),
            "items": [
                {
                    "variant_id": str(basket_env["variant"].public_id),
                    "required_quantity": "600",
                    "unit": "g",
                }
            ],
        },
    )
    assert resp.status_code == 200, resp.text
    line = resp.json()["lines"][0]
    assert line["packages"] == 2
    assert line["list_cost"] == "2.4"  # before promo
    assert line["line_cost"] == "1.2"  # 2x1 -> charge for ceil(2/2)=1 pack
    assert line["promotion_applied"] is True
    assert line["promotion"]["type"] == "nxm"


def test_item_without_price_is_unresolved_not_fabricated(
    client: TestClient, basket_env: dict
) -> None:
    db: Session = basket_env["db"]
    # A second variant of the same retailer with NO observation at all.
    external2 = ExternalProduct(retailer_id=basket_env["retailer"].id, external_id="RB-2")
    db.add(external2)
    db.flush()
    priceless = ProductVariant(
        product_id=basket_env["product"].id,
        retailer_id=basket_env["retailer"].id,
        external_product_id=external2.id,
        display_name="Sin Precio 1kg",
        package_quantity=Decimal("1000"),
        package_unit="g",
    )
    db.add(priceless)
    db.flush()

    _auth(client, "rb-unpriced@example.com")
    resp = client.post(
        "/api/v1/prices/resolve-basket",
        json={
            "store_id": str(basket_env["store"].public_id),
            "items": [
                {
                    "variant_id": str(priceless.public_id),
                    "required_quantity": "500",
                    "unit": "g",
                },
                {
                    "ingredient": "no-existe-este-producto-xyz",
                    "required_quantity": "1",
                    "unit": "unit",
                },
            ],
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["lines"] == []  # nothing priced
    reasons = {u["reason"] for u in body["unresolved"]}
    assert reasons == {"no_price", "no_match"}
    # No fabricated cost.
    assert body["totals"]["total_cost"] == "0"
    assert body["coverage"]["resolved_count"] == 0
    assert body["coverage"]["unresolved_count"] == 2
    assert body["coverage"]["coverage_ratio"] == "0"


def test_total_splits_known_vs_estimated(
    client: TestClient, basket_env: dict
) -> None:
    db: Session = basket_env["db"]
    # A second variant priced with an ESTIMATED observation.
    external2 = ExternalProduct(retailer_id=basket_env["retailer"].id, external_id="RB-3")
    db.add(external2)
    db.flush()
    product2 = Product(name="Estimado RB", is_synthetic=True)
    db.add(product2)
    db.flush()
    est_variant = ProductVariant(
        product_id=product2.id,
        retailer_id=basket_env["retailer"].id,
        external_product_id=external2.id,
        display_name="Estimado 500g",
        package_quantity=Decimal("500"),
        package_unit="g",
    )
    db.add(est_variant)
    db.flush()
    observed = datetime.now(UTC) - timedelta(hours=2)
    db.add(
        PriceObservation(
            retailer_id=basket_env["retailer"].id,
            store_id=basket_env["store"].id,
            product_variant_id=est_variant.id,
            price_scope="exact_store",
            price_type="estimated",
            amount=Decimal("2.00"),
            currency="EUR",
            observed_at=observed,
            imported_at=observed,
            valid_from=observed,
            confidence_score=Decimal("0.40"),
        )
    )
    db.flush()

    _auth(client, "rb-split@example.com")
    resp = client.post(
        "/api/v1/prices/resolve-basket",
        json={
            "store_id": str(basket_env["store"].public_id),
            "items": [
                {
                    "variant_id": str(basket_env["variant"].public_id),
                    "required_quantity": "500",
                    "unit": "g",
                },
                {
                    "variant_id": str(est_variant.public_id),
                    "required_quantity": "500",
                    "unit": "g",
                },
            ],
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["lines"]) == 2
    assert body["totals"]["known_cost"] == "1.2"  # the regular-price line
    assert body["totals"]["estimated_cost"] == "2"  # the estimated line
    assert body["totals"]["total_cost"] == "3.2"
    est_line = next(li for li in body["lines"] if li["is_estimated"])
    assert est_line["price_type"] == "estimated"


def test_requires_authentication(client: TestClient, basket_env: dict) -> None:
    resp = client.post(
        "/api/v1/prices/resolve-basket",
        json={
            "store_id": str(basket_env["store"].public_id),
            "items": [
                {
                    "variant_id": str(basket_env["variant"].public_id),
                    "required_quantity": "500",
                    "unit": "g",
                }
            ],
        },
    )
    assert resp.status_code == 401
