"""Deduplicate EXACT-duplicate staging price observations (spec Fase A.4/B/E/F).

Only observations whose FACT identity is identical are removable — the difference is purely
technical (a different row id / imported_at). A real change of price, observed_at, store or scope is
NOT a duplicate and is never touched. Products/variants/prices and reviewed mappings are never
touched. Every apply first writes a fully REVERSIBLE manifest to the audit log, so the removed rows
can be reconstructed exactly.

    python -m cestaplan_api.tools.dedup_staging_observations --provider parsebot-carrefour --dry-run
    python -m cestaplan_api.tools.dedup_staging_observations --provider parsebot-carrefour \
        --apply --expected-delete-count 145 --manifest-path /tmp/manifest.json
    python -m cestaplan_api.tools.dedup_staging_observations --restore-manifest <uuid> --dry-run
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from cestaplan_api.db import SessionLocal
from cestaplan_api.ingestion.providers.onboarding import get_entry
from cestaplan_api.models import AuditLog, PriceObservation, Retailer

_ACTION = "staging_observation_dedup"
_ENTITY = "price_observation_dedup_manifest"
_REASON = "exact_observation_duplicate"

# The FACT identity of a staging observation. imported_at / id are EXCLUDED on purpose — they are
# the only permitted difference between exact duplicates.
_FACT_FIELDS = (
    "retailer_id",
    "product_variant_id",
    "price_scope",
    "price_type",
    "amount",
    "currency",
    "observed_at",
    "store_id",
    "delivery_zone_id",
    "source_id",
)


def _json_default(value: object) -> object:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (bytes, memoryview)):
        return bytes(value).hex()
    if isinstance(value, uuid.UUID):
        return str(value)
    raise TypeError(f"unserializable {type(value).__name__}")


def _row_values(obs: PriceObservation) -> dict[str, Any]:
    """Every column value of the row, JSON-safe, so it can be reconstructed exactly."""
    return {
        c.name: json.loads(json.dumps(getattr(obs, c.name), default=_json_default))
        for c in PriceObservation.__table__.columns
    }


def _fact_key(obs: PriceObservation) -> tuple:
    return tuple(
        json.dumps(getattr(obs, f), default=_json_default, sort_keys=True) for f in _FACT_FIELDS
    )


def _fact_fingerprint(obs: PriceObservation) -> str:
    return hashlib.sha256("|".join(_fact_key(obs)).encode()).hexdigest()[:16]


def _row_hash(values: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


def _retailer_id(db: Session, provider_code: str) -> int | None:
    entry = get_entry(provider_code)
    slug = entry.retailer_slug if entry else provider_code
    return db.scalar(select(Retailer.id).where(Retailer.slug == slug))


def compute_groups(db: Session, provider_code: str) -> list[dict[str, Any]]:
    """Duplicate groups for a provider's retailer. Each group keeps the EARLIEST-imported row as the
    canonical and lists the rest as removable, with the full reversible payload."""
    retailer_id = _retailer_id(db, provider_code)
    if retailer_id is None:
        return []
    obs = (
        db.execute(
            select(PriceObservation).where(
                PriceObservation.retailer_id == retailer_id,
                PriceObservation.staging_only.is_(True),
                PriceObservation.rolled_back_at.is_(None),
            )
        )
        .scalars()
        .all()
    )
    buckets: dict[tuple, list[PriceObservation]] = {}
    for o in obs:
        buckets.setdefault(_fact_key(o), []).append(o)

    groups: list[dict[str, Any]] = []
    for rows in buckets.values():
        if len(rows) < 2:
            continue  # a unique fact is not a duplicate
        # canonical = earliest imported (then lowest id) so history's first sighting is preserved.
        rows.sort(key=lambda r: (r.imported_at, r.id))
        canonical, removed = rows[0], rows[1:]
        groups.append(
            {
                "fact_fingerprint": _fact_fingerprint(canonical),
                "canonical_observation_id": canonical.id,
                "removed_observation_ids": [r.id for r in removed],
                "row_count": len(rows),
                "removed_rows": [
                    {"id": r.id, "row_hash": _row_hash(_row_values(r)), "values": _row_values(r)}
                    for r in removed
                ],
            }
        )
    return groups


def build_report(groups: list[dict[str, Any]]) -> dict[str, Any]:
    removable = sum(len(g["removed_observation_ids"]) for g in groups)
    return {
        "duplicate_groups": len(groups),
        "removable_exact_duplicates": removable,
        "groups": groups,
    }


def dry_run(db: Session, provider_code: str) -> dict[str, Any]:
    return build_report(compute_groups(db, provider_code))


def _persist_manifest(
    db: Session, provider_code: str, groups: list[dict[str, Any]]
) -> uuid.UUID:
    """Write a reversible manifest to the audit log (never only the ephemeral filesystem)."""
    manifest_id = uuid.uuid4()
    db.add(
        AuditLog(
            action=_ACTION,
            entity_type=_ENTITY,
            entity_public_id=manifest_id,
            occurred_at=datetime.now(UTC),
            audit_metadata={
                "provider_code": provider_code,
                "reason": _REASON,
                "groups": groups,
            },
        )
    )
    db.flush()
    return manifest_id


def apply(
    db: Session,
    provider_code: str,
    *,
    expected_delete_count: int,
    manifest_path: str | None,
) -> dict[str, Any]:
    """Delete EXACTLY ``expected_delete_count`` exact-duplicate observations in ONE txn, with
    a reversible manifest written first. Aborts (no writes) on any count/invariant mismatch."""
    groups = compute_groups(db, provider_code)
    removable_ids = [rid for g in groups for rid in g["removed_observation_ids"]]
    if len(removable_ids) != expected_delete_count:
        raise SystemExit(
            f"ABORT: expected {expected_delete_count} deletions, found {len(removable_ids)}. "
            "No changes made."
        )

    # Lock the exact rows to delete so a concurrent write cannot change them mid-operation.
    locked = (
        db.execute(
            select(PriceObservation)
            .where(PriceObservation.id.in_(removable_ids))
            .with_for_update()
        )
        .scalars()
        .all()
    )
    if len(locked) != expected_delete_count:
        raise SystemExit("ABORT: locked row count changed; concurrent modification. Rolled back.")

    manifest_id = _persist_manifest(db, provider_code, groups)
    for o in locked:
        db.delete(o)
    db.flush()

    # In-transaction invariants before commit.
    remaining = build_report(compute_groups(db, provider_code))["removable_exact_duplicates"]
    if remaining != 0:
        db.rollback()
        raise SystemExit(f"ABORT: {remaining} exact duplicates remain after delete. Rolled back.")

    if manifest_path:
        with open(manifest_path, "w") as f:
            json.dump({"manifest_id": str(manifest_id), "groups": groups}, f, indent=2)
    db.commit()
    return {
        "manifest_id": str(manifest_id),
        "deleted_count": expected_delete_count,
        "remaining_exact_duplicates": 0,
    }


def restore_manifest(db: Session, manifest_id: str, *, apply_restore: bool) -> dict[str, Any]:
    """Reconstruct the deleted rows from a manifest. ``apply_restore=False`` proves the payload is
    usable inside a transaction that is then rolled back (never re-inserts permanently)."""
    row = db.execute(
        select(AuditLog).where(
            AuditLog.entity_type == _ENTITY,
            AuditLog.entity_public_id == uuid.UUID(manifest_id),
        )
    ).scalar_one_or_none()
    if row is None or not row.audit_metadata:
        raise SystemExit(f"manifest {manifest_id} not found")
    groups = row.audit_metadata.get("groups", [])
    to_restore = [r for g in groups for r in g["removed_rows"]]
    restored = 0
    for r in to_restore:
        values = dict(r["values"])
        # Reconstruct the exact row, minus the DB-managed id (a fresh row equal in FACT fields).
        values.pop("id", None)
        for k in ("observed_at", "imported_at", "valid_from", "valid_until", "expires_at"):
            if values.get(k):
                values[k] = datetime.fromisoformat(values[k])
        for k in ("amount", "unit_amount", "confidence_score"):
            if values.get(k) is not None:
                values[k] = Decimal(str(values[k]))
        cols = {k: v for k, v in values.items() if hasattr(PriceObservation, k)}
        db.add(PriceObservation(**cols))
        restored += 1
    db.flush()
    result = {
        "manifest_id": manifest_id,
        "restorable_rows": len(to_restore),
        "reconstructed": restored,
    }
    if apply_restore:
        db.commit()
    else:
        db.rollback()  # proof-only: demonstrate reconstruction without re-inserting permanently
    return result


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--provider", default=None)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--apply", action="store_true")
    p.add_argument("--expected-delete-count", type=int, default=None)
    p.add_argument("--manifest-path", default=None)
    p.add_argument("--restore-manifest", default=None)
    a = p.parse_args(argv)

    with SessionLocal() as db:
        if a.restore_manifest:
            out = restore_manifest(db, a.restore_manifest, apply_restore=a.apply)
        elif a.apply:
            if a.expected_delete_count is None:
                raise SystemExit("--apply requires --expected-delete-count (safety gate)")
            if not a.provider:
                raise SystemExit("--apply requires --provider")
            out = apply(
                db,
                a.provider,
                expected_delete_count=a.expected_delete_count,
                manifest_path=a.manifest_path,
            )
        else:
            if not a.provider:
                raise SystemExit("--dry-run requires --provider")
            report = dry_run(db, a.provider)
            out = {
                "mode": "dry-run",
                "duplicate_groups": report["duplicate_groups"],
                "removable_exact_duplicates": report["removable_exact_duplicates"],
            }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
