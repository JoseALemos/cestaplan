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

import logging
import time
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import and_, select, text
from sqlalchemy.orm import Session

from cestaplan_api.models import PriceObservation, PriceObservationOccurrence
from cestaplan_api.services import observation_identity as ident

_LOG = logging.getLogger("cestaplan.observation_persistence")

# Bound every lock wait so a stuck writer can never hang the transaction indefinitely; on timeout
# PostgreSQL raises ``lock_not_available`` (surfaced, not silently swallowed).
_DEFAULT_LOCK_TIMEOUT_MS = 5000

# The occurrence-identity fields come from the SHARED definition: replaying the same tuple must not
# create a duplicate occurrence.
_OCCURRENCE_IDENTITY: tuple[str, ...] = ident.OCCURRENCE_IDENTITY_FIELDS

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
    # Concurrency diagnostics (spec §6): how much serialization actually happened.
    total_lock_wait_ms: int = 0
    facts_reused_after_lock: int = 0
    occurrences_reused_after_lock: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "observations_created": self.observations_created,
            "observations_reused": self.observations_reused,
            "occurrences_created": self.occurrences_created,
            "occurrences_reused": self.occurrences_reused,
            "total_lock_wait_ms": self.total_lock_wait_ms,
            "facts_reused_after_lock": self.facts_reused_after_lock,
            "occurrences_reused_after_lock": self.occurrences_reused_after_lock,
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
class LockDiagnostics:
    """Sanitized per-call concurrency diagnostics (spec §6) — only lock keys (hashes) + timings.

    Never carries payloads, URLs or secrets. Lock keys are non-reversible fingerprints.
    """

    fact_lock_key: int
    occurrence_lock_key: int | None = None
    fact_lock_acquired: bool = False
    occurrence_lock_acquired: bool = False
    lock_wait_ms: int = 0
    fact_reused_after_lock: bool = False
    occurrence_reused_after_lock: bool = False


@dataclass(slots=True)
class RecordResult:
    observation: PriceObservation
    occurrence: PriceObservationOccurrence
    fact_created: bool
    occurrence_created: bool
    diagnostics: LockDiagnostics | None = None


def _eq(column, value):
    return column.is_(None) if value is None else column == value


def _set_lock_timeout(db: Session, timeout_ms: int) -> None:
    """Bound lock waits for this transaction. ``timeout_ms`` is an int we control (no injection)."""
    db.execute(text(f"SET LOCAL lock_timeout = '{int(timeout_ms)}ms'"))


def _advisory_xact_lock(db: Session, key: int) -> int:
    """Acquire a transactional advisory lock (released at commit/rollback). Returns the wait in ms.

    Blocks up to the transaction ``lock_timeout``; on timeout PostgreSQL raises
    ``lock_not_available`` which propagates (contention is surfaced, never an infinite wait).
    """
    start = time.monotonic()
    db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": key})
    return int((time.monotonic() - start) * 1000)


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
    lock_timeout_ms: int = _DEFAULT_LOCK_TIMEOUT_MS,
) -> RecordResult:
    """Idempotently record ``candidate`` as a fact + one provenance occurrence, serialized.

    Concurrency (spec §2/§6): two writers of the SAME fact (or occurrence) are serialized with
    PostgreSQL transactional advisory locks keyed by the shared fingerprints, so the search-then-
    insert can never race into a duplicate:

    1. acquire the FACT lock (fingerprint-keyed) → re-search under the lock → create only if
       still absent;
    2. acquire the OCCURRENCE lock (fingerprint-keyed) → re-search the occurrence → create only if
       still absent.

    Locks are always taken fact-first then occurrence — a fixed global order that prevents deadlocks
    — and are released automatically at commit/rollback. A NULL provenance field equals another
    NULL (shared occurrence identity), so re-confirming a fact never duplicates its occurrence.
    """
    metrics = metrics or RecordMetrics()
    # Server-default identity fields are None on a transient object; coerce so the candidate's
    # fingerprint matches a persisted, refreshed row (requires_loyalty defaults to False in the DB).
    if candidate.requires_loyalty is None:
        candidate.requires_loyalty = False

    _set_lock_timeout(db, lock_timeout_ms)
    diag = LockDiagnostics(fact_lock_key=ident.fact_lock_key(candidate))

    # ---- Fact: serialize on the fact fingerprint, then re-search under the lock ----
    diag.lock_wait_ms += _advisory_xact_lock(db, diag.fact_lock_key)
    diag.fact_lock_acquired = True
    staging_only = bool(candidate.staging_only)
    existing = _find_existing_fact(db, candidate, staging_only=staging_only)
    if existing is not None:
        fact = existing
        fact_created = False
        diag.fact_reused_after_lock = True
        metrics.observations_reused += 1
        metrics.facts_reused_after_lock += 1
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

    # ---- Occurrence: serialize on the occurrence fingerprint (fact.id is known now) ----
    occ_identity = {
        "price_observation_id": fact.id,
        "provider_code": provenance.provider_code,
        "source_id": provenance.source_id,
        "crawl_run_id": provenance.crawl_run_id,
        "raw_capture_id": provenance.raw_capture_id,
        "connector_version": provenance.connector_version,
        "parser_version": provenance.parser_version,
    }
    diag.occurrence_lock_key = ident.occurrence_lock_key(occ_identity)
    diag.lock_wait_ms += _advisory_xact_lock(db, diag.occurrence_lock_key)
    diag.occurrence_lock_acquired = True
    occurrence = _find_existing_occurrence(db, fact.id, provenance)
    if occurrence is not None:
        occurrence_created = False
        diag.occurrence_reused_after_lock = True
        metrics.occurrences_reused += 1
        metrics.occurrences_reused_after_lock += 1
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

    metrics.total_lock_wait_ms += diag.lock_wait_ms
    # Sanitized diagnostics only (lock keys are non-reversible hashes; no payloads/URLs/secrets).
    _LOG.debug(
        "record_price_fact fact_lock=%s occ_lock=%s wait_ms=%d fact_reused=%s occ_reused=%s",
        diag.fact_lock_acquired,
        diag.occurrence_lock_acquired,
        diag.lock_wait_ms,
        diag.fact_reused_after_lock,
        diag.occurrence_reused_after_lock,
    )
    return RecordResult(
        observation=fact,
        occurrence=occurrence,
        fact_created=fact_created,
        occurrence_created=occurrence_created,
        diagnostics=diag,
    )


__all__ = [
    "LockDiagnostics",
    "OccurrenceProvenance",
    "RecordMetrics",
    "RecordResult",
    "record_price_fact",
]
