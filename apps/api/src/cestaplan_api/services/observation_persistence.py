"""Idempotent two-layer persistence of a price fact + its provenance occurrence (spec §3/§4).

The single shared entry point used by sync (and, later, any other writer) so the two-layer model is
enforced in ONE place:

1. Compute the economic-fact fingerprint of the candidate (via the shared
   :mod:`cestaplan_api.services.observation_identity`).
2. Look for an existing ``PriceObservation`` with the SAME fingerprint (scoped to the same
   staging/production routing). If found -> reuse it (``observations_reused``); if not -> persist
   the candidate as a new fact (``observations_created``) and close the prior open history row.
3. Upsert exactly ONE ``PriceObservationOccurrence`` for this provenance. Replaying the identical
   occurrence (same crawl/capture/parser/source) does NOT duplicate it (``occurrences_reused``); a
   new crawl or new parser producing the same fact adds a new occurrence (``occurrences_created``).

Spec §4 in one line: a change to any of the 16 fact-identity fields is a NEW fact; a new
crawl/capture/parser reporting the SAME fact is a new OCCURRENCE, never a new fact.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from cestaplan_api.models import PriceObservation, PriceObservationOccurrence
from cestaplan_api.services import observation_identity as ident

# The occurrence-identity fields: replaying the same tuple must not create a duplicate occurrence.
_OCCURRENCE_IDENTITY: tuple[str, ...] = (
    "price_observation_id",
    "provider_code",
    "source_id",
    "crawl_run_id",
    "raw_capture_id",
    "connector_version",
    "parser_version",
)

# Selective subset of the fact identity used to PREFILTER candidate rows before the exact
# fingerprint comparison (keeps the query cheap; the fingerprint is the source of truth).
_FACT_PREFILTER: tuple[str, ...] = (
    "retailer_id",
    "product_variant_id",
    "store_id",
    "price_scope",
    "price_type",
    "amount",
    "currency",
    "observed_at",
)


@dataclass(slots=True)
class RecordMetrics:
    """Counters emitted by :func:`record_price_fact`, aggregated across a run (spec §3/§9)."""

    observations_created: int = 0
    observations_reused: int = 0
    occurrences_created: int = 0
    occurrences_reused: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "observations_created": self.observations_created,
            "observations_reused": self.observations_reused,
            "occurrences_created": self.occurrences_created,
            "occurrences_reused": self.occurrences_reused,
        }


@dataclass(slots=True)
class OccurrenceProvenance:
    """The provenance of ONE occasion a fact was confirmed — never secrets or raw payloads."""

    provider_code: str | None = None
    source_id: int | None = None
    source_url: str | None = None
    crawl_run_id: int | None = None
    raw_capture_id: int | None = None
    connector_version: str | None = None
    parser_version: str | None = None
    confidence_score: Decimal | None = None
    verification_status: str = "unverified"
    evidence_fingerprint: str | None = None


@dataclass(slots=True)
class RecordResult:
    observation: PriceObservation
    occurrence: PriceObservationOccurrence
    fact_created: bool
    occurrence_created: bool


def _eq(column, value):
    return column.is_(None) if value is None else column == value


def _find_existing_fact(
    db: Session, candidate: PriceObservation, *, staging_only: bool
) -> PriceObservation | None:
    """Return an existing fact with the SAME fingerprint, or None. Prefilters on the selective
    identity subset (+ staging routing) then confirms via the full 16-field fingerprint."""
    conditions = [
        _eq(getattr(PriceObservation, f), getattr(candidate, f)) for f in _FACT_PREFILTER
    ]
    conditions.append(PriceObservation.staging_only.is_(staging_only))
    target = ident.price_fact_fingerprint(candidate)
    rows = (
        db.execute(select(PriceObservation).where(and_(*conditions))).scalars().all()
    )
    for row in rows:
        if ident.price_fact_fingerprint(row) == target:
            return row
    return None


def _close_prior_open_row(
    db: Session, candidate: PriceObservation, *, closed_by_run_id: int | None
) -> None:
    """Append-only history: close the current open row for the same (variant, store, scope) whose
    validity started at or before this observation, so a genuinely new fact supersedes it."""
    prior = (
        db.execute(
            select(PriceObservation)
            .where(
                PriceObservation.product_variant_id == candidate.product_variant_id,
                _eq(PriceObservation.store_id, candidate.store_id),
                PriceObservation.price_scope == candidate.price_scope,
                PriceObservation.staging_only.is_(candidate.staging_only),
                PriceObservation.valid_until.is_(None),
                PriceObservation.rolled_back_at.is_(None),
            )
            .order_by(PriceObservation.valid_from.desc())
        )
        .scalars()
        .first()
    )
    if prior is not None and prior.valid_from <= candidate.observed_at:
        prior.valid_until = candidate.observed_at
        prior.closed_by_run_id = closed_by_run_id


def _find_existing_occurrence(
    db: Session, observation_id: int, provenance: OccurrenceProvenance
) -> PriceObservationOccurrence | None:
    values = {
        "price_observation_id": observation_id,
        "provider_code": provenance.provider_code,
        "source_id": provenance.source_id,
        "crawl_run_id": provenance.crawl_run_id,
        "raw_capture_id": provenance.raw_capture_id,
        "connector_version": provenance.connector_version,
        "parser_version": provenance.parser_version,
    }
    conditions = [
        _eq(getattr(PriceObservationOccurrence, f), values[f]) for f in _OCCURRENCE_IDENTITY
    ]
    return (
        db.execute(select(PriceObservationOccurrence).where(and_(*conditions)))
        .scalars()
        .first()
    )


def record_price_fact(
    db: Session,
    candidate: PriceObservation,
    provenance: OccurrenceProvenance,
    *,
    imported_at: datetime,
    metrics: RecordMetrics | None = None,
) -> RecordResult:
    """Idempotently record ``candidate`` as an economic fact + one provenance occurrence.

    ``candidate`` is an UNSAVED :class:`PriceObservation` carrying the fact-identity fields. If a
    fact with the same fingerprint already exists (same staging routing) it is REUSED — the
    candidate is not persisted and no history is closed; otherwise the candidate becomes a new fact
    and the prior open history row is closed. Either way exactly one occurrence is upserted.
    """
    metrics = metrics or RecordMetrics()
    # Server-default identity fields are None on a transient object; coerce so the candidate's
    # fingerprint matches a persisted, refreshed row (requires_loyalty defaults to False in the DB).
    if candidate.requires_loyalty is None:
        candidate.requires_loyalty = False

    staging_only = bool(candidate.staging_only)
    existing = _find_existing_fact(db, candidate, staging_only=staging_only)
    if existing is not None:
        fact = existing
        fact_created = False
        metrics.observations_reused += 1
    else:
        _close_prior_open_row(db, candidate, closed_by_run_id=provenance.crawl_run_id)
        candidate.imported_at = candidate.imported_at or imported_at
        if candidate.valid_from is None:
            candidate.valid_from = candidate.observed_at
        db.add(candidate)
        db.flush()
        fact = candidate
        fact_created = True
        metrics.observations_created += 1

    occurrence = _find_existing_occurrence(db, fact.id, provenance)
    if occurrence is not None:
        occurrence_created = False
        metrics.occurrences_reused += 1
    else:
        occurrence = PriceObservationOccurrence(
            price_observation_id=fact.id,
            provider_code=provenance.provider_code,
            source_id=provenance.source_id,
            source_url=provenance.source_url,
            crawl_run_id=provenance.crawl_run_id,
            raw_capture_id=provenance.raw_capture_id,
            connector_version=provenance.connector_version,
            parser_version=provenance.parser_version,
            imported_at=imported_at,
            confidence_score=provenance.confidence_score,
            verification_status=provenance.verification_status,
            evidence_fingerprint=provenance.evidence_fingerprint,
        )
        db.add(occurrence)
        db.flush()
        occurrence_created = True
        metrics.occurrences_created += 1

    return RecordResult(
        observation=fact,
        occurrence=occurrence,
        fact_created=fact_created,
        occurrence_created=occurrence_created,
    )


__all__ = [
    "OccurrenceProvenance",
    "RecordMetrics",
    "RecordResult",
    "record_price_fact",
]
