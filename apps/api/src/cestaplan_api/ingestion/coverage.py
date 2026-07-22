"""Price-coverage measurement for the ingestion pipeline (FASE A, Task 3).

:class:`PriceCoverageService` computes an honest snapshot of how much of a retailer's (or a
store's) catalogue actually has usable prices, and persists it as a :class:`CoverageSnapshot`.

Honesty is the point: partial coverage is reported as :class:`CoverageStatus.PARTIAL` (or
worse), never dressed up as complete. Counts are derived from the append-only
:class:`PriceObservation` history against the discovered :class:`ProductVariant` catalogue,
and freshness reuses the same ``stale_price_hours`` / ``expired_price_hours`` thresholds as
the current-price read. Ratios are :class:`decimal.Decimal`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from cestaplan_api.config import get_settings
from cestaplan_api.ingestion import CoverageStatus
from cestaplan_api.models import CoverageSnapshot, PriceObservation, ProductVariant

_DEFAULT_STALE_HOURS = 24
_DEFAULT_EXPIRED_HOURS = 48
_RATIO_QUANT = Decimal("0.0001")


@dataclass(frozen=True, slots=True)
class _VariantPrice:
    """The latest valid observation for one variant, reduced to what coverage needs."""

    amount: Decimal
    price_type: str
    available: bool | None
    age_hours: float


def _thresholds() -> tuple[int, int]:
    settings = get_settings()
    stale = int(getattr(settings, "stale_price_hours", _DEFAULT_STALE_HOURS))
    expired = int(getattr(settings, "expired_price_hours", _DEFAULT_EXPIRED_HOURS))
    return stale, expired


def _ratio(numerator: Decimal, denominator: Decimal) -> Decimal | None:
    if denominator <= 0:
        return None
    return (numerator / denominator).quantize(_RATIO_QUANT)


class PriceCoverageService:
    """Computes and persists coverage snapshots for a retailer/store."""

    def snapshot(
        self,
        db: Session,
        retailer_id: int,
        *,
        store_id: int | None = None,
        as_of: datetime,
    ) -> CoverageSnapshot:
        """Compute coverage counts/ratios, grade a status and persist a snapshot row."""
        stale_hours, _expired_hours = _thresholds()

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
        discovered = len(variant_ids)

        priced = 0
        fresh = 0
        stale = 0
        estimated = 0
        unavailable = 0
        priced_value = Decimal("0")
        fresh_value = Decimal("0")

        for variant_id in variant_ids:
            latest = self._latest_valid(
                db, variant_id, store_id=store_id, as_of=as_of
            )
            if latest is None:
                continue
            priced += 1
            priced_value += latest.amount
            if latest.price_type == "estimated":
                estimated += 1
            if latest.available is False:
                unavailable += 1
            if latest.age_hours < stale_hours:
                fresh += 1
                fresh_value += latest.amount
            else:
                stale += 1

        # No external "expected" figure yet: the discovered catalogue is our expectation.
        expected = discovered
        coverage_ratio = _ratio(Decimal(priced), Decimal(expected))
        weighted_coverage_ratio = _ratio(fresh_value, priced_value)
        status = self._grade(
            expected=expected, priced=priced, fresh=fresh, ratio=coverage_ratio
        )

        snapshot = CoverageSnapshot(
            retailer_id=retailer_id,
            store_id=store_id,
            observed_at=as_of,
            expected_products=expected,
            discovered_products=discovered,
            priced_products=priced,
            fresh_prices=fresh,
            stale_prices=stale,
            estimated_prices=estimated,
            unavailable_products=unavailable,
            coverage_ratio=coverage_ratio,
            weighted_coverage_ratio=weighted_coverage_ratio,
            status=status.value,
        )
        db.add(snapshot)
        db.flush()
        return snapshot

    def latest_coverage(
        self, db: Session, retailer_id: int, store_id: int | None = None
    ) -> CoverageSnapshot | None:
        """The most recent persisted coverage snapshot for a retailer/store."""
        stmt = (
            select(CoverageSnapshot)
            .where(CoverageSnapshot.retailer_id == retailer_id)
            .order_by(CoverageSnapshot.observed_at.desc(), CoverageSnapshot.id.desc())
            .limit(1)
        )
        if store_id is None:
            stmt = stmt.where(CoverageSnapshot.store_id.is_(None))
        else:
            stmt = stmt.where(CoverageSnapshot.store_id == store_id)
        return db.execute(stmt).scalars().first()

    # -- internals ------------------------------------------------------------ #

    def _latest_valid(
        self,
        db: Session,
        product_variant_id: int,
        *,
        store_id: int | None,
        as_of: datetime,
    ) -> _VariantPrice | None:
        stmt = (
            select(PriceObservation)
            .where(
                PriceObservation.product_variant_id == product_variant_id,
                PriceObservation.verification_status != "disputed",
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
        obs = db.execute(stmt).scalars().first()
        if obs is None:
            return None
        age_hours = (as_of - obs.observed_at).total_seconds() / 3600.0
        return _VariantPrice(
            amount=obs.amount,
            price_type=obs.price_type,
            available=obs.available,
            age_hours=age_hours,
        )

    @staticmethod
    def _grade(
        *, expected: int, priced: int, fresh: int, ratio: Decimal | None
    ) -> CoverageStatus:
        if expected <= 0 or priced <= 0:
            return CoverageStatus.NONE
        if fresh <= 0:
            return CoverageStatus.STALE
        if ratio is None:
            return CoverageStatus.NONE
        if ratio >= Decimal("1"):
            return CoverageStatus.COMPLETE
        if ratio >= Decimal("0.9"):
            return CoverageStatus.HIGH
        if ratio >= Decimal("0.5"):
            return CoverageStatus.PARTIAL
        return CoverageStatus.INSUFFICIENT


__all__ = ["PriceCoverageService"]
