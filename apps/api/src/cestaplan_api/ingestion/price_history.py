"""Append-only price history recording for the ingestion pipeline (FASE A, Task 1).

:func:`record_observation` turns a resolved :class:`NormalizedObservation` into an entry in
the append-only :class:`PriceObservation` history. It never destructively rewrites a price:

- **Change** (different amount or promotion) closes the prior open interval
  (``valid_until = as_of``) and INSERTs a fresh open row (``valid_from = as_of``,
  ``valid_until = None``), tagged with the crawl_run / raw_capture that detected the change.
  Full history is preserved.
- **No change** does not duplicate the row; it revalidates the open row in place by bumping
  its ``observed_at`` (and ``imported_at``) so we keep evidence it was re-seen without
  exploding history. Idempotent: re-recording the same amount only bumps the timestamp.
- **Quarantined / anomalous** observations never auto-replace the last-good open row. The
  observation is stored as a closed, disputed row and linked to a :class:`PriceAnomaly`;
  the prior good data is left intact so the current-price read still returns it.

Money is :class:`decimal.Decimal` throughout. The function flushes but never commits — the
caller owns the transaction.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from cestaplan_api.ingestion import NormalizedObservation, Severity
from cestaplan_api.models import PriceAnomaly, PriceObservation


def _promotion_text(obs: NormalizedObservation) -> str | None:
    """The comparable promotion text carried by an observation, or ``None``."""
    if obs.promotion is None:
        return None
    return obs.promotion.raw_text


def _latest_open(
    db: Session,
    *,
    product_variant_id: int,
    store_id: int | None,
    price_scope: str,
    price_type: str,
) -> PriceObservation | None:
    """The current open (``valid_until IS NULL``) observation for this identity, if any."""
    stmt = (
        select(PriceObservation)
        .where(
            PriceObservation.product_variant_id == product_variant_id,
            PriceObservation.price_scope == price_scope,
            PriceObservation.price_type == price_type,
            PriceObservation.valid_until.is_(None),
            PriceObservation.verification_status != "disputed",
        )
        .order_by(PriceObservation.valid_from.desc(), PriceObservation.id.desc())
        .limit(1)
    )
    if store_id is None:
        stmt = stmt.where(PriceObservation.store_id.is_(None))
    else:
        stmt = stmt.where(PriceObservation.store_id == store_id)
    return db.execute(stmt).scalars().first()


def _build_row(
    obs: NormalizedObservation,
    *,
    product_variant_id: int,
    retailer_id: int,
    store_id: int | None,
    as_of: datetime,
    valid_until: datetime | None,
    verification_status: str,
    source_id: int | None,
    crawl_run_id: int | None,
    raw_capture_id: int | None,
) -> PriceObservation:
    """Materialize a :class:`PriceObservation` row from a normalized observation."""
    promotion = obs.promotion
    return PriceObservation(
        retailer_id=retailer_id,
        store_id=store_id,
        product_variant_id=product_variant_id,
        price_scope=obs.price_scope.value,
        price_type=obs.price_type.value,
        amount=obs.amount,
        currency=obs.currency,
        unit_amount=obs.unit_amount,
        unit_code=obs.unit_code,
        promotion_text=promotion.raw_text if promotion is not None else None,
        requires_loyalty=obs.requires_loyalty,
        promotion_valid_from=promotion.valid_from if promotion is not None else None,
        promotion_valid_until=promotion.valid_until if promotion is not None else None,
        available=obs.available,
        source_id=source_id,
        source_url=obs.source.source_url if obs.source is not None else None,
        observed_at=obs.observed_at,
        imported_at=as_of,
        valid_from=as_of,
        valid_until=valid_until,
        confidence_score=obs.confidence,
        raw_capture_id=raw_capture_id,
        crawl_run_id=crawl_run_id,
        connector_version=obs.source.connector_version if obs.source is not None else None,
        parser_version=obs.source.parser_version if obs.source is not None else None,
        verification_status=verification_status,
    )


def record_observation(
    db: Session,
    obs: NormalizedObservation,
    *,
    product_variant_id: int,
    retailer_id: int,
    as_of: datetime,
    store_id: int | None = None,
    source_id: int | None = None,
    crawl_run_id: int | None = None,
    raw_capture_id: int | None = None,
    quarantined: bool = False,
    anomaly_type: str = "quarantined",
    anomaly_severity: Severity = Severity.HIGH,
) -> PriceObservation:
    """Record a normalized observation into the append-only price history.

    The observation must already be resolved to a concrete ``product_variant_id`` /
    ``retailer_id`` / ``store_id``; the amount, scope, type and promotion come from ``obs``.
    ``as_of`` is the effective instant the pipeline processed this observation.

    Returns the :class:`PriceObservation` row that represents this call: the new open row on
    a change, the revalidated open row when unchanged, or the closed disputed row when
    ``quarantined``.
    """
    price_scope = obs.price_scope.value
    price_type = obs.price_type.value
    current = _latest_open(
        db,
        product_variant_id=product_variant_id,
        store_id=store_id,
        price_scope=price_scope,
        price_type=price_type,
    )

    # Quarantined observations never replace last-good: store as a closed, disputed row and
    # link an anomaly, leaving any prior open row untouched.
    if quarantined:
        row = _build_row(
            obs,
            product_variant_id=product_variant_id,
            retailer_id=retailer_id,
            store_id=store_id,
            as_of=as_of,
            valid_until=as_of,
            verification_status="disputed",
            source_id=source_id,
            crawl_run_id=crawl_run_id,
            raw_capture_id=raw_capture_id,
        )
        db.add(row)
        db.flush()
        db.add(
            PriceAnomaly(
                price_observation_id=row.id,
                crawl_run_id=crawl_run_id,
                anomaly_type=anomaly_type,
                severity=anomaly_severity.value,
                actual_value=obs.amount,
                status="quarantined",
            )
        )
        db.flush()
        return row

    # Unchanged re-observation: revalidate the open row in place, do not duplicate history.
    if current is not None and _is_unchanged(current, obs):
        current.observed_at = obs.observed_at
        current.imported_at = as_of
        db.flush()
        return current

    # Change (or first observation): close the prior open interval and append a new open row.
    if current is not None:
        current.valid_until = as_of

    row = _build_row(
        obs,
        product_variant_id=product_variant_id,
        retailer_id=retailer_id,
        store_id=store_id,
        as_of=as_of,
        valid_until=None,
        verification_status="unverified",
        source_id=source_id,
        crawl_run_id=crawl_run_id,
        raw_capture_id=raw_capture_id,
    )
    db.add(row)
    db.flush()
    return row


def _is_unchanged(current: PriceObservation, obs: NormalizedObservation) -> bool:
    """Whether ``obs`` carries the same price identity as the open ``current`` row."""
    return (
        current.amount == obs.amount
        and current.currency == obs.currency
        and current.promotion_text == _promotion_text(obs)
        and current.requires_loyalty == obs.requires_loyalty
    )


__all__ = ["record_observation"]
