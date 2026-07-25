"""Single source of truth for the ECONOMIC PRICE FACT identity (two-layer model, spec §2).

Layer A — ``PriceObservation`` is one unique economic fact (a price for a variant in a scope at an
instant). Layer B — ``PriceObservationOccurrence`` records each time a provider/crawl/parser
confirmed that fact. Which crawl/parser recorded a fact is PROVENANCE, not part of the fact, so a
re-sync (new crawl_run) or a new parser producing the SAME data is a new OCCURRENCE, never a new
fact. Only a change to an identity field below is a new fact.

This module is the ONE shared definition used by idempotent persistence, sync, discovery, dedup,
metrics, the cleanup tool and the tests — never a second divergent implementation.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from cestaplan_api.models import PriceObservation

# The 16 fields that DEFINE the economic fact. A change to ANY of these is a new PriceObservation.
FACT_FIELDS: tuple[str, ...] = (
    "retailer_id",
    "store_id",
    "delivery_zone_id",
    "product_variant_id",
    "price_scope",
    "price_type",
    "amount",
    "currency",
    "unit_amount",
    "unit_code",
    "promotion_text",  # normalized (trimmed) below
    "requires_loyalty",
    "promotion_valid_from",
    "promotion_valid_until",
    "available",
    "observed_at",
)

# Every other column, with the reason it is NOT part of the fact identity. Provenance/technical
# metadata (recorded on the occurrence) or append-only lifecycle bookkeeping.
EXCLUDED_FIELDS: dict[str, str] = {
    "id": "row identity",
    "public_id": "row identity",
    "imported_at": "when WE recorded it, not the fact",
    "created_at": "row bookkeeping",
    "updated_at": "row bookkeeping",
    "source_id": "provenance (which source)",
    "source_url": "provenance (where from)",
    "crawl_run_id": "provenance (which crawl)",
    "raw_capture_id": "provenance (which capture)",
    "connector_version": "provenance (which connector)",
    "parser_version": "provenance (which parser)",
    "confidence_score": "provenance quality, not the fact",
    "verification_status": "review state, not the fact",
    "valid_from": "append-only history bookkeeping",
    "valid_until": "append-only history bookkeeping",
    "expires_at": "TTL, not the fact",
    "rolled_back_at": "lifecycle bookkeeping",
    "rolled_back_by": "lifecycle bookkeeping",
    "closed_by_run_id": "append-only history bookkeeping",
    "staging_only": "staging/production routing flag; queries already scope by it, not the fact",
}


def all_columns() -> tuple[str, ...]:
    return tuple(c.name for c in PriceObservation.__table__.columns)


def unclassified_columns() -> tuple[str, ...]:
    """Columns that are neither a fact field nor an explicitly-excluded one — a guard test fails
    when a new column appears, so it is CONSCIOUSLY classified rather than silently folded in."""
    classified = set(FACT_FIELDS) | set(EXCLUDED_FIELDS)
    return tuple(c for c in all_columns() if c not in classified)


def _normalize(field: str, value: object) -> object:
    if field == "promotion_text" and isinstance(value, str):
        return value.strip()
    return value


def _json_default(value: object) -> object:
    if isinstance(value, datetime):
        # Same instant -> same string: normalize to UTC so +00:00 and +02:00 never look different.
        aware = value.astimezone(UTC) if value.tzinfo is not None else value.replace(tzinfo=UTC)
        return aware.isoformat()
    if isinstance(value, Decimal):
        # Value-equal decimals must be string-equal: 1.19 == 1.1900, 1.0 == 1.0000 (no sci form).
        return format(value.normalize(), "f")
    if isinstance(value, (bytes, memoryview)):
        return bytes(value).hex()
    if isinstance(value, uuid.UUID):
        return str(value)
    raise TypeError(f"unserializable {type(value).__name__}")


def price_fact_identity(obs: PriceObservation) -> tuple:
    return tuple(
        json.dumps(_normalize(f, getattr(obs, f)), default=_json_default, sort_keys=True)
        for f in FACT_FIELDS
    )


def price_fact_fingerprint(obs: PriceObservation) -> str:
    return hashlib.sha256("|".join(price_fact_identity(obs)).encode()).hexdigest()


def row_values(obs: PriceObservation) -> dict[str, Any]:
    """Every column value of the row, JSON-safe, so it can be reconstructed exactly."""
    return {
        c: json.loads(json.dumps(getattr(obs, c), default=_json_default)) for c in all_columns()
    }


def row_hash(values: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


# --------------------------------------------------------------------------- #
# Occurrence (Layer B) identity — which run/parser/source confirmed a fact (spec §3).
# --------------------------------------------------------------------------- #
# The FULL occurrence identity. ``imported_at`` is deliberately NOT part of it (it is WHEN we
# recorded the occurrence, not what distinguishes it). NULL semantics: a missing field equals
# another missing field — two occurrences with the same non-null values AND the same NULLs are the
# SAME occurrence (reused), because the fingerprint serializes ``None`` to one canonical token
# (``"null"``). A different crawl/parser/capture/source yields a different fingerprint -> a new
# occurrence.
OCCURRENCE_IDENTITY_FIELDS: tuple[str, ...] = (
    "price_observation_id",
    "provider_code",
    "source_id",
    "crawl_run_id",
    "raw_capture_id",
    "connector_version",
    "parser_version",
)
# The provenance sub-tuple (identity minus the fact it points at); dedup compares the evidence two
# rows carry independently of which observation currently owns them.
OCCURRENCE_PROVENANCE_FIELDS: tuple[str, ...] = OCCURRENCE_IDENTITY_FIELDS[1:]


def _field(source: Any, field: str) -> Any:
    if isinstance(source, Mapping):
        return source.get(field)
    return getattr(source, field, None)


def occurrence_identity(source: Any) -> tuple[str, ...]:
    """Occurrence identity as JSON tokens (NULL-safe: ``None`` -> the single token ``"null"``).

    ``source`` is anything exposing the identity fields — a ``PriceObservationOccurrence``, an
    ``OccurrenceProvenance`` (plus ``price_observation_id``), or a plain mapping.
    """
    return tuple(
        json.dumps(_field(source, f), default=_json_default, sort_keys=True)
        for f in OCCURRENCE_IDENTITY_FIELDS
    )


def occurrence_fingerprint(source: Any) -> str:
    return hashlib.sha256("|".join(occurrence_identity(source)).encode()).hexdigest()


def occurrence_provenance_tuple(source: Any) -> tuple[Any, ...]:
    """The raw provenance values (NULLs preserved) used for equality comparisons in dedup."""
    return tuple(_field(source, f) for f in OCCURRENCE_PROVENANCE_FIELDS)


def signed_bigint(fingerprint_hex: str) -> int:
    """Deterministic signed 64-bit int for a PostgreSQL advisory-lock key, from a hex fingerprint.

    Uses the (SHA-256) fingerprint bytes directly and maps to the signed ``bigint`` range — NEVER
    Python ``hash()`` (its salt changes between processes, so keys would not agree across writers).
    """
    return int.from_bytes(bytes.fromhex(fingerprint_hex)[:8], "big", signed=True)


def fact_lock_key(obs: PriceObservation) -> int:
    """Stable advisory-lock key for a price fact (from its fingerprint)."""
    return signed_bigint(price_fact_fingerprint(obs))


def occurrence_lock_key(source: Any) -> int:
    """Stable advisory-lock key for an occurrence (from its fingerprint)."""
    return signed_bigint(occurrence_fingerprint(source))


# Back-compat aliases (the dedup tool + tests use these names).
fact_key = price_fact_identity
fact_fingerprint = price_fact_fingerprint


def semantic_columns() -> tuple[str, ...]:
    """The fact-identity columns (kept for callers that report which columns define the fact)."""
    return FACT_FIELDS


# Kept so the cleanup tool's manifest can list what it excluded.
TECHNICAL_FIELDS = frozenset(EXCLUDED_FIELDS)


__all__ = [
    "EXCLUDED_FIELDS",
    "FACT_FIELDS",
    "OCCURRENCE_IDENTITY_FIELDS",
    "OCCURRENCE_PROVENANCE_FIELDS",
    "TECHNICAL_FIELDS",
    "all_columns",
    "fact_fingerprint",
    "fact_key",
    "fact_lock_key",
    "occurrence_fingerprint",
    "occurrence_identity",
    "occurrence_lock_key",
    "occurrence_provenance_tuple",
    "price_fact_fingerprint",
    "price_fact_identity",
    "row_hash",
    "row_values",
    "semantic_columns",
    "signed_bigint",
    "unclassified_columns",
]
