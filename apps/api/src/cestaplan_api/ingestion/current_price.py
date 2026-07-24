"""Current price + freshness projection for the ingestion pipeline (FASE A, Task 2).

:class:`CurrentPriceService` reads the append-only :class:`PriceObservation` history and
answers "what is the price right now?" honestly:

- :meth:`CurrentPriceService.current` returns the latest VALID observation for a variant at a
  store/scope, enriched with its **age** and a freshness ``status`` (``fresh|stale|expired|
  unknown``) computed from ``stale_price_hours`` / ``expired_price_hours``.
- :meth:`CurrentPriceService.current_for_retailer` mirrors the per-chain pricing decision:
  the latest observation per variant across the whole chain.
- :meth:`CurrentPriceService.project_current_prices` projects the newest valid observations
  into the legacy :class:`ProductPrice` table so the meal-plan engine keeps working. It is
  append-only friendly (a ``(store, product, observed_at)`` row already present is skipped)
  and never fabricates: a variant without a canonical product, a store, or a valid price is
  simply left without a projected price.

Money is :class:`decimal.Decimal` throughout. Reads/upserts flush but never commit.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.orm import Session

from cestaplan_api.config import get_settings
from cestaplan_api.models import (
    DataSource,
    PriceObservation,
    ProductPrice,
    ProductVariant,
)

_DEFAULT_STALE_HOURS = 24
_DEFAULT_EXPIRED_HOURS = 48
# source_type carried onto ProductPrice when the observation has no linked DataSource.
_DEFAULT_SOURCE_TYPE = "community_connector"
_DEFAULT_SOURCE_NAME = "ingestion"


class FreshnessStatus(StrEnum):
    """Freshness grade of a current price relative to the stale/expired thresholds."""

    FRESH = "fresh"
    STALE = "stale"
    EXPIRED = "expired"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class CurrentPrice:
    """The current price for a variant with provenance and freshness metadata."""

    product_variant_id: int
    amount: Decimal
    currency: str
    price_scope: str
    price_type: str
    store_id: int | None
    delivery_zone_id: int | None
    source_id: int | None
    observed_at: datetime
    age: timedelta
    status: FreshnessStatus
    confidence: Decimal
    promotion_text: str | None
    available: bool | None


def _thresholds() -> tuple[int, int]:
    """`(stale_hours, expired_hours)` from settings, with safe defaults if unset."""
    settings = get_settings()
    stale = int(getattr(settings, "stale_price_hours", _DEFAULT_STALE_HOURS))
    expired = int(getattr(settings, "expired_price_hours", _DEFAULT_EXPIRED_HOURS))
    return stale, expired


def _freshness(age: timedelta, *, stale_hours: int, expired_hours: int) -> FreshnessStatus:
    """Grade an age against the stale/expired hour thresholds."""
    hours = age.total_seconds() / 3600.0
    if hours < 0:
        # Observed in the future relative to as_of; treat as fresh, not stale.
        return FreshnessStatus.FRESH
    if hours < stale_hours:
        return FreshnessStatus.FRESH
    if hours < expired_hours:
        return FreshnessStatus.STALE
    return FreshnessStatus.EXPIRED


class CurrentPriceService:
    """Reads current prices from append-only history and projects them for the engine."""

    def current(
        self,
        db: Session,
        product_variant_id: int,
        *,
        store_id: int | None = None,
        scope: str | None = None,
        as_of: datetime,
        staging: bool = False,
    ) -> CurrentPrice | None:
        """Latest VALID observation for a variant at a store/scope, or ``None`` if none.

        ``staging=False`` (default) is the production view and NEVER sees ``staging_only`` rows.
        ``staging=True`` reads ONLY staging rows, for shadow/coverage evaluation — never used to
        cost a production plan.
        """
        obs = self._latest_valid(
            db, product_variant_id, store_id=store_id, scope=scope, staging=staging
        )
        if obs is None:
            return None
        return self._to_current(obs, as_of=as_of)

    def current_for_retailer(
        self,
        db: Session,
        retailer_id: int,
        *,
        store_id: int | None = None,
        as_of: datetime,
    ) -> list[CurrentPrice]:
        """Latest observation per variant across the chain (the per-chain price decision)."""
        variant_ids = (
            db.execute(
                select(ProductVariant.id).where(
                    ProductVariant.retailer_id == retailer_id,
                    ProductVariant.active.is_(True),
                )
            )
            .scalars()
            .all()
        )
        results: list[CurrentPrice] = []
        for variant_id in variant_ids:
            current = self.current(
                db, variant_id, store_id=store_id, as_of=as_of
            )
            if current is not None:
                results.append(current)
        return results

    def project_current_prices(self, db: Session, retailer_id: int) -> int:
        """Upsert ``ProductPrice`` from newest valid observations. Returns rows written.

        Append-only friendly: a ``(store, product, observed_at)`` price already present is
        skipped. Never fabricates — a variant without a canonical product, without a store,
        or without a valid observation is left without a projected price.
        """
        variants = (
            db.execute(
                select(ProductVariant).where(
                    ProductVariant.retailer_id == retailer_id,
                    ProductVariant.active.is_(True),
                )
            )
            .scalars()
            .all()
        )
        written = 0
        for variant in variants:
            if variant.product_id is None:
                continue
            obs = self._latest_valid(db, variant.id)
            if obs is None or obs.store_id is None:
                continue
            if self._price_exists(db, obs.store_id, variant.product_id, obs.observed_at):
                continue
            source_type, source_name = self._source_provenance(db, obs.source_id)
            db.add(
                ProductPrice(
                    retailer_id=obs.retailer_id,
                    store_id=obs.store_id,
                    product_id=variant.product_id,
                    amount=obs.amount,
                    currency=obs.currency,
                    package_quantity=variant.package_quantity
                    if variant.package_quantity is not None
                    else Decimal("1"),
                    package_unit=variant.package_unit or "unit",
                    unit_price=obs.unit_amount,
                    promotion=obs.promotion_text,
                    availability=self._availability(obs.available),
                    source_type=source_type,
                    source_name=source_name,
                    source_url=obs.source_url,
                    observed_at=obs.observed_at,
                    imported_at=datetime.now(obs.observed_at.tzinfo),
                    confidence_score=obs.confidence_score,
                    is_synthetic=False,
                )
            )
            written += 1
        db.flush()
        return written

    # -- internals ------------------------------------------------------------ #

    def _latest_valid(
        self,
        db: Session,
        product_variant_id: int,
        *,
        store_id: int | None = None,
        scope: str | None = None,
        staging: bool = False,
    ) -> PriceObservation | None:
        stmt = (
            select(PriceObservation)
            .where(
                PriceObservation.product_variant_id == product_variant_id,
                PriceObservation.verification_status != "disputed",
                # Production view excludes staging imports (§P); staging view reads only them.
                PriceObservation.staging_only.is_(staging),
            )
            .order_by(
                PriceObservation.valid_until.is_(None).desc(),
                PriceObservation.observed_at.desc(),
                PriceObservation.id.desc(),
            )
            .limit(1)
        )
        if store_id is not None:
            stmt = stmt.where(PriceObservation.store_id == store_id)
        if scope is not None:
            stmt = stmt.where(PriceObservation.price_scope == scope)
        return db.execute(stmt).scalars().first()

    def _to_current(self, obs: PriceObservation, *, as_of: datetime) -> CurrentPrice:
        stale_hours, expired_hours = _thresholds()
        age = as_of - obs.observed_at
        status = _freshness(age, stale_hours=stale_hours, expired_hours=expired_hours)
        return CurrentPrice(
            product_variant_id=obs.product_variant_id,
            amount=obs.amount,
            currency=obs.currency,
            price_scope=obs.price_scope,
            price_type=obs.price_type,
            store_id=obs.store_id,
            delivery_zone_id=obs.delivery_zone_id,
            source_id=obs.source_id,
            observed_at=obs.observed_at,
            age=age,
            status=status,
            confidence=obs.confidence_score,
            promotion_text=obs.promotion_text,
            available=obs.available,
        )

    def _price_exists(
        self, db: Session, store_id: int, product_id: int, observed_at: datetime
    ) -> bool:
        existing = db.execute(
            select(ProductPrice.id).where(
                ProductPrice.store_id == store_id,
                ProductPrice.product_id == product_id,
                ProductPrice.observed_at == observed_at,
            )
        ).first()
        return existing is not None

    def _source_provenance(self, db: Session, source_id: int | None) -> tuple[str, str]:
        if source_id is None:
            return _DEFAULT_SOURCE_TYPE, _DEFAULT_SOURCE_NAME
        source = db.get(DataSource, source_id)
        if source is None:
            return _DEFAULT_SOURCE_TYPE, _DEFAULT_SOURCE_NAME
        return source.source_type, source.name

    @staticmethod
    def _availability(available: bool | None) -> str | None:
        if available is None:
            return None
        return "in_stock" if available else "out_of_stock"


__all__ = ["CurrentPrice", "CurrentPriceService", "FreshnessStatus"]
