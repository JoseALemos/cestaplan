"""Single source of truth for a staging ``PriceObservation``'s FACT identity (spec §1/§7).

Shared by idempotent persistence (A.1), duplicate detection, metrics and the cleanup tool — there is
never a second, divergent definition. The identity is EVERY model column EXCEPT a small allowlist of
technical (row-bookkeeping) fields, derived from the live model, so a newly-added column is part of
the fact by default (conservative) and a guard test fails until it is consciously classified.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from cestaplan_api.models import PriceObservation

# The ONLY differences allowed between two rows that are otherwise the SAME fact: pure row
# bookkeeping. Everything else must be identical for two rows to be exact duplicates.
TECHNICAL_FIELDS: frozenset[str] = frozenset(
    {"id", "imported_at", "created_at", "updated_at", "public_id"}
)


def _json_default(value: object) -> object:
    if isinstance(value, datetime):
        # Same instant -> same string: normalize to UTC so +00:00 and +02:00 never look different.
        aware = value.astimezone(UTC) if value.tzinfo is not None else value.replace(tzinfo=UTC)
        return aware.isoformat()
    if isinstance(value, Decimal):
        # Value-equal decimals must be string-equal: 1.19 == 1.1900, 1.0 == 1.0000 (no sci notation).
        return format(value.normalize(), "f")
    if isinstance(value, (bytes, memoryview)):
        return bytes(value).hex()
    if isinstance(value, uuid.UUID):
        return str(value)
    raise TypeError(f"unserializable {type(value).__name__}")


def all_columns() -> tuple[str, ...]:
    return tuple(c.name for c in PriceObservation.__table__.columns)


def semantic_columns() -> tuple[str, ...]:
    """Non-technical columns from the LIVE model (never a stale hardcoded list)."""
    return tuple(c for c in all_columns() if c not in TECHNICAL_FIELDS)


def row_values(obs: PriceObservation) -> dict[str, Any]:
    """Every column value of the row, JSON-safe, so it can be reconstructed exactly."""
    return {
        c: json.loads(json.dumps(getattr(obs, c), default=_json_default)) for c in all_columns()
    }


def fact_key(obs: PriceObservation) -> tuple:
    return tuple(
        json.dumps(getattr(obs, c), default=_json_default, sort_keys=True)
        for c in semantic_columns()
    )


def fact_fingerprint(obs: PriceObservation) -> str:
    return hashlib.sha256("|".join(fact_key(obs)).encode()).hexdigest()


def row_hash(values: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


__all__ = [
    "TECHNICAL_FIELDS",
    "all_columns",
    "fact_fingerprint",
    "fact_key",
    "row_hash",
    "row_values",
    "semantic_columns",
]
