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

from cestaplan_api.models import PriceAnomaly, PriceObservation, PriceObservationOccurrence
from cestaplan_api.services import observation_identity as ident

_LOG = logging.getLogger("cestaplan.observation_persistence")

# Bound every lock wait so a stuck writer can never hang the transaction indefinitely; on timeout
# PostgreSQL raises ``lock_not_available`` (surfaced, not silently swallowed).
_DEFAULT_LOCK_TIMEOUT_MS = 5000

# A same-timestamp conflict marks its facts disputed (never a current price) — see §7 policy below.
_DISPUTED = "disputed"
_SAME_TIMESTAMP_CONFLICT = "same_timestamp_conflict"

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
    # Concurrency + temporal diagnostics (spec §6/§8): how much serialization/history work happened.
    total_lock_wait_ms: int = 0
    facts_reused_after_lane_lock: int = 0
    occurrences_reused_after_lock: int = 0
    out_of_order_inserts: int = 0
    same_timestamp_conflicts: int = 0
    blocked_gaps: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "observations_created": self.observations_created,
            "observations_reused": self.observations_reused,
            "occurrences_created": self.occurrences_created,
            "occurrences_reused": self.occurrences_reused,
            "total_lock_wait_ms": self.total_lock_wait_ms,
            "facts_reused_after_lane_lock": self.facts_reused_after_lane_lock,
            "occurrences_reused_after_lock": self.occurrences_reused_after_lock,
            "out_of_order_inserts": self.out_of_order_inserts,
            "same_timestamp_conflicts": self.same_timestamp_conflicts,
            "blocked_gaps": self.blocked_gaps,
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

    lane_lock_key: int
    occurrence_lock_key: int | None = None
    lane_lock_acquired: bool = False
    occurrence_lock_acquired: bool = False
    lane_lock_wait_ms: int = 0
    lock_wait_ms: int = 0
    fact_reused_after_lane_lock: bool = False
    occurrence_reused_after_lock: bool = False
    temporal_predecessor_found: bool = False
    temporal_successor_found: bool = False
    out_of_order_insert: bool = False
    same_timestamp_conflict: bool = False
    blocked_gap_before: bool = False


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


def _lane_conditions(candidate: PriceObservation) -> list:
    """SQL conditions selecting the candidate's history lane (the shared LANE_FIELDS)."""
    return [_eq(getattr(PriceObservation, f), getattr(candidate, f)) for f in ident.LANE_FIELDS]


def _lane_rows(db: Session, candidate: PriceObservation) -> list[PriceObservation]:
    """ALL non-rolled-back rows of the lane (active AND disputed). Disputed rows are temporal
    ANCHORS/barriers even though they never enter the active price chain, so placement must see
    them."""
    conds = _lane_conditions(candidate)
    conds.append(PriceObservation.rolled_back_at.is_(None))
    return list(db.execute(select(PriceObservation).where(and_(*conds))).scalars())


def _add_conflict_anomaly(db: Session, observation_id: int) -> None:
    db.add(
        PriceAnomaly(
            price_observation_id=observation_id,
            anomaly_type=_SAME_TIMESTAMP_CONFLICT,
            severity="high",
        )
    )
    db.flush()


def _place_in_temporal_sequence(
    db: Session, candidate: PriceObservation, *, closed_by_run_id: int | None, diag: LockDiagnostics
) -> None:
    """Insert ``candidate`` into its lane's interval chain by observed_at ``T`` (spec §4/§7).

    Called ONLY under the lane lock, so the read + placement is atomic per lane. Sets the
    candidate's ``valid_from``/``valid_until`` from its true predecessor/successor (not merely
    "the open row") so out-of-order arrivals slot between neighbours and never leave two open rows.

    Same-timestamp conflict (§7, policy B): if a DIFFERENT fact already sits at exactly ``T`` in
    this lane, every same-``T`` fact — the newcomer and any still-active sibling — is marked
    ``disputed`` with an empty ``[T, T]`` interval (never a "current" price) and flagged with an
    anomaly.

    ANCHOR placement (§1/§2): a disputed timestamp is a temporal BARRIER. Placement uses the nearest
    ANCHORS (any non-rolled-back row's timestamp, active or disputed), never merely the nearest
    active row, so a new fact never extends a prior interval across a conflict and never spans a
    barrier: ``valid_until`` is the next anchor, the predecessor is closed only when the immediate
    previous anchor is a NON-disputed active row, and a disputed previous anchor leaves a gap.
    """
    t = candidate.observed_at
    candidate.valid_from = t
    rows = _lane_rows(db, candidate)  # active + disputed anchors
    at_t = [r for r in rows if r.valid_from == t]
    if at_t:  # candidate is a NEW distinct fact (an identical one would have been reused)
        candidate.valid_until = t  # empty interval -> never current
        candidate.verification_status = _DISPUTED
        for e in at_t:
            if e.verification_status != _DISPUTED:
                e.verification_status = _DISPUTED
                e.valid_until = e.valid_from  # collapse the ambiguous sibling to empty too
                _add_conflict_anomaly(db, e.id)
        diag.same_timestamp_conflict = True
        return

    disputed_ts = {r.valid_from for r in rows if r.verification_status == _DISPUTED}
    before = [r.valid_from for r in rows if r.valid_from < t]
    after = [r.valid_from for r in rows if r.valid_from > t]
    previous_anchor = max(before) if before else None
    next_anchor = min(after) if after else None

    # Never cross the next anchor (if it is a disputed barrier, end exactly ON it).
    candidate.valid_until = next_anchor
    # Extend the predecessor ONLY when the immediate previous anchor is a non-disputed active row.
    # A disputed previous anchor is a barrier: leave the interval before it closed, gap after it.
    if previous_anchor is not None and previous_anchor not in disputed_ts:
        for r in rows:
            if r.valid_from == previous_anchor and r.verification_status != _DISPUTED:
                r.valid_until = t
                r.closed_by_run_id = closed_by_run_id
        diag.temporal_predecessor_found = True
    diag.temporal_successor_found = next_anchor is not None
    diag.out_of_order_insert = next_anchor is not None
    diag.blocked_gap_before = previous_anchor is not None and previous_anchor in disputed_ts


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

    Concurrency (spec §2/§3/§6): the search-then-insert can never race into a duplicate OR into two
    open history rows, because writers serialize on PostgreSQL transactional advisory locks in a
    fixed global order:

    1. acquire the HISTORY-LANE lock (keyed by the lane fingerprint). Two facts that differ only by
       amount/observed_at belong to the SAME lane, so this — not the per-fact fingerprint — is what
       serializes the interval-chain update. Under the lane lock: re-search the exact fact and reuse
       it if present; otherwise place the new fact into its temporal sequence
       (predecessor/successor, §4) before inserting. No decisive lane read happens before the lock.
    2. acquire the OCCURRENCE lock (keyed by the occurrence fingerprint) → re-search → upsert.

    The lane lock replaces a separate fact lock: two identical facts necessarily share a lane, so
    the lane lock already serializes their search/create. Fixed order (lane, then occurrence) cannot
    deadlock — no writer ever takes the occurrence lock before the lane lock. Both release at
    commit/rollback. A NULL provenance field equals another NULL, so re-confirming a fact never
    duplicates its occurrence.
    """
    metrics = metrics or RecordMetrics()
    # Server-default identity fields are None on a transient object; coerce so the candidate's
    # fingerprint matches a persisted, refreshed row (requires_loyalty defaults to False in the DB).
    if candidate.requires_loyalty is None:
        candidate.requires_loyalty = False

    _set_lock_timeout(db, lock_timeout_ms)
    diag = LockDiagnostics(lane_lock_key=ident.price_history_lane_lock_key(candidate))

    # ---- Fact: serialize on the HISTORY LANE, then re-search + place under the lock ----
    diag.lane_lock_wait_ms = _advisory_xact_lock(db, diag.lane_lock_key)
    diag.lock_wait_ms += diag.lane_lock_wait_ms
    diag.lane_lock_acquired = True
    staging_only = bool(candidate.staging_only)
    existing = _find_existing_fact(db, candidate, staging_only=staging_only)
    if existing is not None:
        fact = existing
        fact_created = False
        diag.fact_reused_after_lane_lock = True
        metrics.observations_reused += 1
        metrics.facts_reused_after_lane_lock += 1
    else:
        _place_in_temporal_sequence(
            db, candidate, closed_by_run_id=provenance.crawl_run_id, diag=diag
        )
        candidate.imported_at = candidate.imported_at or imported_at
        db.add(candidate)
        db.flush()
        if diag.same_timestamp_conflict:
            _add_conflict_anomaly(db, candidate.id)
            metrics.same_timestamp_conflicts += 1
        if diag.out_of_order_insert:
            metrics.out_of_order_inserts += 1
        if diag.blocked_gap_before:
            metrics.blocked_gaps += 1
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
        "record_price_fact lane_lock=%s occ_lock=%s wait_ms=%d fact_reused=%s occ_reused=%s "
        "pred=%s succ=%s ooo=%s same_ts_conflict=%s blocked_gap=%s",
        diag.lane_lock_acquired,
        diag.occurrence_lock_acquired,
        diag.lock_wait_ms,
        diag.fact_reused_after_lane_lock,
        diag.occurrence_reused_after_lock,
        diag.temporal_predecessor_found,
        diag.temporal_successor_found,
        diag.out_of_order_insert,
        diag.same_timestamp_conflict,
        diag.blocked_gap_before,
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
