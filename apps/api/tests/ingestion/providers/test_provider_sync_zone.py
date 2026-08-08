"""The write path stamps a zonified observation with its delivery-zone store (multi-zona support).

A product carrying a ``postal_code`` (Mercadona) is resolved to the chain's zone Store and the
resulting :class:`PriceObservation` is stamped with that ``store_id`` (part of the lane/fact
identity). A national product (no ``postal_code``) keeps ``store_id=None`` — unchanged behaviour.
Fully synthetic, no network.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from cestaplan_api.ingestion.contracts import PriceScope
from cestaplan_api.ingestion.providers.contracts import (
    Availability,
    ExternalCatalogProduct,
    SellUnit,
)
from cestaplan_api.models import CrawlRun, PriceObservation, Retailer, Store
from cestaplan_api.services.observation_persistence import RecordMetrics
from cestaplan_api.services.provider_sync import _append_observation, _upsert_variant

_NOW = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)


def _mercadona_product(postal_code: str | None) -> ExternalCatalogProduct:
    return ExternalCatalogProduct(
        provider="apify-mercadona",
        retailer_slug="mercadona",
        external_product_id="10381",
        product_name="Leche semidesnatada Hacendado",
        sell_unit=SellUnit.PACKAGE,
        regular_price=Decimal("5.04"),
        currency="EUR",
        price_scope=PriceScope.POSTAL_CODE if postal_code else PriceScope.UNKNOWN,
        postal_code=postal_code,
        observed_at=_NOW,
        availability=Availability.IN_STOCK,
        variable_weight=False,
        unit_price=Decimal("0.84"),
        unit_price_unit="l",
    )


def _setup(db: Session, slug: str) -> tuple[int, int]:
    retailer = Retailer(slug=slug, name=slug, adapter_key="apify-mercadona", is_synthetic=True)
    db.add(retailer)
    db.flush()
    run = CrawlRun(retailer_id=retailer.id, run_type="prices", status="completed")
    db.add(run)
    db.flush()
    return retailer.id, run.id


def test_staging_observation_is_stamped_with_zone_store(db_session: Session) -> None:
    rid, run_id = _setup(db_session, "mercadona-zone")
    product = _mercadona_product("14006")
    variant = _upsert_variant(db_session, rid, product)
    _append_observation(
        db_session, rid, variant, product, run_id, True, _NOW, RecordMetrics()
    )

    zone = db_session.execute(
        select(Store).where(Store.retailer_id == rid, Store.postal_code == "14006")
    ).scalar_one()
    obs = db_session.execute(
        select(PriceObservation).where(PriceObservation.product_variant_id == variant.id)
    ).scalar_one()
    assert obs.store_id == zone.id  # the zonified price is owned by its delivery-zone store
    assert obs.price_scope == "postal_code"


def test_national_observation_keeps_no_store(db_session: Session) -> None:
    rid, run_id = _setup(db_session, "mercadona-nozone")
    product = _mercadona_product(None)  # no postal code -> national/unknown, no zone
    variant = _upsert_variant(db_session, rid, product)
    _append_observation(
        db_session, rid, variant, product, run_id, True, _NOW, RecordMetrics()
    )

    obs = db_session.execute(
        select(PriceObservation).where(PriceObservation.product_variant_id == variant.id)
    ).scalar_one()
    assert obs.store_id is None  # unchanged: no postal code -> no zone store
    # ...and no zone store was created for this retailer.
    assert (
        db_session.execute(
            select(Store).where(Store.retailer_id == rid)
        ).scalars().first()
        is None
    )
