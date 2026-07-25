"""Reversible cleanup of EXACT-duplicate staging price observations (spec Fase A.4/B/§1-§6).

A row is removable ONLY when EVERY non-technical column is identical to the group's canonical row
(the fact identity lives in ``services.observation_identity`` and is shared with idempotent
persistence, dedup detection and metrics). Two rows may differ ONLY in technical bookkeeping (id,
imported_at, created_at, updated_at, public_id). A change of any other column is a distinct fact
and is never touched.

Additional safety gates before a row can be deleted:
* PROVENANCE (§2): the row must be provably a ``<provider>`` row — via ``crawl_run_id`` → a CrawlRun
  for the provider's retailer, or ``source_id`` → a DataSource. Ambiguous provenance -> excluded.
* INCOMING FK REFERENCES (§3): a row referenced by PromotionRule / PriceAnomaly is excluded, and its
  presence among the removable set ABORTS the apply.

``--apply`` is gated on ``--expected-delete-count`` and runs in one transaction (rows locked FOR
UPDATE, revalidated against the plan), writes a rich REVERSIBLE manifest to the audit log FIRST, and
rolls back on any mismatch. ``--restore-manifest`` performs an EXACT restore (original id preserved)
and verifies the restored row hash equals the original.

    python -m cestaplan_api.tools.dedup_staging_observations --provider parsebot-carrefour --dry-run
    python -m cestaplan_api.tools.dedup_staging_observations --provider parsebot-carrefour \
        --apply --expected-delete-count 145 --manifest-path /tmp/m.json
    python -m cestaplan_api.tools.dedup_staging_observations --restore-manifest <uuid>
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Boolean, DateTime, Numeric, select, update
from sqlalchemy.orm import Session

from cestaplan_api.db import SessionLocal
from cestaplan_api.ingestion.providers.onboarding import get_entry
from cestaplan_api.models import (
    CrawlRun,
    DataSource,
    PriceAnomaly,
    PriceObservation,
    PriceObservationOccurrence,
    PromotionRule,
    Retailer,
)
from cestaplan_api.services import observation_identity as ident

_ACTION = "staging_observation_dedup"
_ENTITY = "price_observation_dedup_manifest"
_REASON = "exact_observation_duplicate"
_SCHEMA_VERSION = 2  # v2: fact-identity groups + occurrence relink (two-layer model, spec §6/§7)
_TOOL_VERSION = "3.0.0"

# Incoming FK references to price_observation.id that BLOCK deletion — a referenced row is never
# auto-deleted. PriceObservationOccurrence is NOT here: its rows are OUR provenance and are relinked
# to the canonical observation before deletion (spec §6/§7), never lost.
_REFERENCING = (
    ("promotion_rule", PromotionRule, PromotionRule.price_observation_id),
    ("price_anomaly", PriceAnomaly, PriceAnomaly.price_observation_id),
)

# The provenance tuple of an occurrence (its identity minus price_observation_id): two occurrences
# with the same tuple are the SAME evidence, so relinking one onto a canonical that already has it
# is a de-duplication, not a loss.
_OCC_PROVENANCE = (
    "provider_code",
    "source_id",
    "crawl_run_id",
    "raw_capture_id",
    "connector_version",
    "parser_version",
)


def _occurrences(db: Session, observation_id: int) -> list[PriceObservationOccurrence]:
    return list(
        db.execute(
            select(PriceObservationOccurrence).where(
                PriceObservationOccurrence.price_observation_id == observation_id
            )
        ).scalars()
    )


def _provenance_tuple(occ: PriceObservationOccurrence) -> tuple:
    return tuple(getattr(occ, f) for f in _OCC_PROVENANCE)


def _occurrence_values(occ: PriceObservationOccurrence) -> dict[str, Any]:
    cols = PriceObservationOccurrence.__table__.columns
    return {c.name: json.loads(json.dumps(getattr(occ, c.name), default=str)) for c in cols}


def _deployed_sha() -> str:
    return os.environ.get("RAILWAY_GIT_COMMIT_SHA") or os.environ.get("GIT_SHA") or "unknown"


def _retailer_id(db: Session, provider_code: str) -> int | None:
    entry = get_entry(provider_code)
    slug = entry.retailer_slug if entry else provider_code
    return db.scalar(select(Retailer.id).where(Retailer.slug == slug))


def _provenance(db: Session, obs: PriceObservation, retailer_id: int) -> str:
    """'verified_crawl_run' | 'verified_source' | 'ambiguous'; retailer alone is NOT enough (§2)."""
    if obs.crawl_run_id is not None:
        run_retailer = db.scalar(
            select(CrawlRun.retailer_id).where(CrawlRun.id == obs.crawl_run_id)
        )
        if run_retailer == retailer_id:
            return "verified_crawl_run"
    if obs.source_id is not None and db.scalar(
        select(DataSource.id).where(DataSource.id == obs.source_id)
    ) is not None:
        return "verified_source"
    return "ambiguous"


def _references(db: Session, observation_id: int) -> list[str]:
    """Referencing tables that point at this observation (empty when safe to delete)."""
    hits: list[str] = []
    for name, model, col in _REFERENCING:
        if db.scalar(select(model.id).where(col == observation_id).limit(1)) is not None:
            hits.append(name)
    return hits


def analyze(db: Session, provider_code: str) -> dict[str, Any]:
    """Full read-only analysis: duplicate groups, per-row provenance + references, exclusions and
    the final removable set. Never writes."""
    retailer_id = _retailer_id(db, provider_code)
    result: dict[str, Any] = {
        "provider_code": provider_code,
        "retailer_id": retailer_id,
        "duplicate_groups": 0,
        "removable_exact_duplicates": 0,
        "rows_with_verified_provider": 0,
        "rows_with_ambiguous_provider": 0,
        "excluded_ambiguous_rows": 0,
        "referenced_rows": 0,
        "reference_tables": [],
        "excluded_due_to_references": 0,
        # Two-layer (spec §6) vocabulary — occurrences are relinked to the canonical fact, not lost.
        "duplicate_fact_groups": 0,
        "removable_price_observations": 0,
        "occurrences_to_relink": 0,
        "occurrences_already_present": 0,
        "ambiguous_provenance": 0,
        "rows_with_fk_dependencies": 0,
        "excluded_groups": 0,
        "unique_fact_count": 0,
        "staging_observations": 0,
        "total_duplicate_fact_groups": 0,
        "total_duplicate_observations": 0,
        "new_real_count": 0,
        "groups": [],
    }
    if retailer_id is None:
        return result

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
        buckets.setdefault(ident.fact_key(o), []).append(o)
    result["staging_observations"] = len(obs)
    result["unique_fact_count"] = len(buckets)
    # ALL duplicates by fact identity (independent of provenance/FK safety); the removable set below
    # is the safely-deletable SUBSET of these.
    result["total_duplicate_fact_groups"] = sum(1 for r in buckets.values() if len(r) > 1)
    result["total_duplicate_observations"] = sum(len(r) - 1 for r in buckets.values() if len(r) > 1)

    ref_tables: set[str] = set()
    for rows in buckets.values():
        if len(rows) < 2:
            continue
        rows.sort(key=lambda r: (r.imported_at, r.id))
        canonical, removed = rows[0], rows[1:]
        # Provenance already on the canonical fact — relinking an identical occurrence is a no-op.
        canonical_prov = {_provenance_tuple(o) for o in _occurrences(db, canonical.id)}
        group_removable: list[dict[str, Any]] = []
        group_excluded = False
        for r in removed:
            prov = _provenance(db, r, retailer_id)
            refs = _references(db, r.id)
            if prov == "ambiguous":
                result["rows_with_ambiguous_provider"] += 1
                result["ambiguous_provenance"] += 1
                result["excluded_ambiguous_rows"] += 1
                group_excluded = True
                continue
            result["rows_with_verified_provider"] += 1
            if refs:
                result["referenced_rows"] += 1
                result["rows_with_fk_dependencies"] += 1
                result["excluded_due_to_references"] += 1
                ref_tables.update(refs)
                group_excluded = True
                continue
            # Plan the occurrence relink: move provenance the canonical lacks, drop duplicates.
            occ_relink: list[dict[str, Any]] = []
            occ_dupe: list[dict[str, Any]] = []
            for occ in _occurrences(db, r.id):
                payload = {"id": occ.id, "values": _occurrence_values(occ)}
                if _provenance_tuple(occ) in canonical_prov:
                    occ_dupe.append(payload)
                    result["occurrences_already_present"] += 1
                else:
                    canonical_prov.add(_provenance_tuple(occ))
                    occ_relink.append(payload)
                    result["occurrences_to_relink"] += 1
            values = ident.row_values(r)
            group_removable.append(
                {
                    "id": r.id,
                    "row_hash": ident.row_hash(values),
                    "provenance": prov,
                    "values": values,
                    "occurrences_to_relink": occ_relink,
                    "occurrences_to_drop": occ_dupe,
                }
            )
        if not group_removable:
            if group_excluded:
                result["excluded_groups"] += 1
            continue
        result["duplicate_groups"] += 1
        result["removable_exact_duplicates"] += len(group_removable)
        result["groups"].append(
            {
                "fact_fingerprint": ident.fact_fingerprint(canonical),
                "canonical_observation_id": canonical.id,
                "canonical_row": ident.row_values(canonical),
                "canonical_row_hash": ident.row_hash(ident.row_values(canonical)),
                "removed_observation_ids": [g["id"] for g in group_removable],
                "removed_rows": group_removable,
            }
        )
    result["reference_tables"] = sorted(ref_tables)
    result["duplicate_fact_groups"] = result["duplicate_groups"]
    result["removable_price_observations"] = result["removable_exact_duplicates"]
    # After dedup each duplicate group collapses to its canonical fact.
    result["new_real_count"] = result["staging_observations"] - result["removable_exact_duplicates"]
    return result


def dry_run(db: Session, provider_code: str) -> dict[str, Any]:
    a = analyze(db, provider_code)
    return {k: v for k, v in a.items() if k != "groups"} | {"mode": "dry-run"}


def _manifest_payload(provider_code: str, analysis: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": _SCHEMA_VERSION,
        "tool_version": _TOOL_VERSION,
        "deployed_commit_sha": _deployed_sha(),
        "provider_code": provider_code,
        "reason": _REASON,
        "generated_at": datetime.now(UTC).isoformat(),
        "technical_columns_excluded": sorted(ident.TECHNICAL_FIELDS),
        "semantic_columns": list(ident.semantic_columns()),
        "reference_tables_checked": [name for name, _m, _c in _REFERENCING],
        "counts": {
            "removable": analysis["removable_exact_duplicates"],
            "removable_price_observations": analysis["removable_price_observations"],
            "duplicate_fact_groups": analysis["duplicate_fact_groups"],
            "occurrences_to_relink": analysis["occurrences_to_relink"],
            "occurrences_already_present": analysis["occurrences_already_present"],
            "excluded_ambiguous": analysis["excluded_ambiguous_rows"],
            "excluded_referenced": analysis["excluded_due_to_references"],
            "excluded_groups": analysis["excluded_groups"],
            "new_real_count": analysis["new_real_count"],
        },
        "groups": analysis["groups"],
    }


def apply(
    db: Session,
    provider_code: str,
    *,
    expected_delete_count: int,
    manifest_path: str | None,
) -> dict[str, Any]:
    """Delete EXACTLY expected_delete_count verified, unreferenced exact duplicates in one txn."""
    analysis = analyze(db, provider_code)
    removable_ids = [rid for g in analysis["groups"] for rid in g["removed_observation_ids"]]
    if len(removable_ids) != expected_delete_count:
        raise SystemExit(
            f"ABORT: expected {expected_delete_count} deletions, found {len(removable_ids)} "
            f"(ambiguous={analysis['excluded_ambiguous_rows']}, "
            f"referenced={analysis['excluded_due_to_references']}). No changes made."
        )

    # Lock the exact rows, then REVALIDATE identity/hash/references against the plan (§4).
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
        raise SystemExit("ABORT: locked row count changed (concurrent modification). No changes.")
    planned = {g["id"]: g for grp in analysis["groups"] for g in grp["removed_rows"]}
    by_id = {o.id: o for o in locked}
    for oid, plan in planned.items():
        o = by_id.get(oid)
        if o is None or ident.row_hash(ident.row_values(o)) != plan["row_hash"]:
            raise SystemExit(f"ABORT: row {oid} changed since dry-run. No changes.")
        if _references(db, oid):
            raise SystemExit(f"ABORT: row {oid} gained a dependent reference. No changes.")

    # §6/§7: relink every occurrence to the canonical fact BEFORE deleting the duplicate row, so no
    # crawl/capture evidence is ever lost. Occurrences the canonical already has (same provenance
    # tuple) are dropped as exact duplicates; the rest are moved. Record both for exact_restore.
    for grp in analysis["groups"]:
        canonical_id = grp["canonical_observation_id"]
        canonical_prov = {_provenance_tuple(o) for o in _occurrences(db, canonical_id)}
        for removed in grp["removed_rows"]:
            relinked: list[dict[str, Any]] = []
            dropped: list[dict[str, Any]] = []
            for occ in _occurrences(db, removed["id"]):
                if _provenance_tuple(occ) in canonical_prov:
                    dropped.append({"id": occ.id, "values": _occurrence_values(occ)})
                    db.delete(occ)
                else:
                    canonical_prov.add(_provenance_tuple(occ))
                    relinked.append({"id": occ.id, "original_observation_id": removed["id"]})
                    occ.price_observation_id = canonical_id
            removed["occurrences_relinked"] = relinked
            removed["occurrences_dropped"] = dropped
    db.flush()

    manifest_id = uuid.uuid4()
    from cestaplan_api.models import AuditLog

    db.add(
        AuditLog(
            action=_ACTION,
            entity_type=_ENTITY,
            entity_public_id=manifest_id,
            occurred_at=datetime.now(UTC),
            audit_metadata=_manifest_payload(provider_code, analysis),
        )
    )
    db.flush()
    for o in locked:
        # Guard: never delete a row that still has occurrences pointing at it (would lose evidence).
        if _occurrences(db, o.id):
            db.rollback()
            raise SystemExit(f"ABORT: row {o.id} still has occurrences after relink. Rolled back.")
        db.delete(o)
    db.flush()

    post = analyze(db, provider_code)
    if post["removable_exact_duplicates"] != 0:
        db.rollback()
        raise SystemExit("ABORT: exact duplicates remain after delete. Rolled back.")
    orphaned = db.scalar(
        select(PriceObservationOccurrence.id)
        .where(PriceObservationOccurrence.price_observation_id.in_(removable_ids))
        .limit(1)
    )
    if orphaned is not None:
        db.rollback()
        raise SystemExit("ABORT: an occurrence still references a deleted row. Rolled back.")

    if manifest_path:
        with open(manifest_path, "w") as f:
            json.dump(
                {"manifest_id": str(manifest_id), **_manifest_payload(provider_code, analysis)},
                f,
                indent=2,
            )
    db.commit()
    relinked_total = sum(
        len(r.get("occurrences_relinked", []))
        for g in analysis["groups"]
        for r in g["removed_rows"]
    )
    dropped_total = sum(
        len(r.get("occurrences_dropped", []))
        for g in analysis["groups"]
        for r in g["removed_rows"]
    )
    return {
        "manifest_id": str(manifest_id),
        "deleted_count": expected_delete_count,
        "occurrences_relinked": relinked_total,
        "occurrences_dropped": dropped_total,
        "remaining_exact_duplicates": 0,
    }


def _coerce(column_name: str, value: object) -> object:
    """Convert a JSON-decoded value back to the column's Python type (dynamic, from the model)."""
    if value is None:
        return None
    col = PriceObservation.__table__.columns[column_name]
    ctype = col.type
    if isinstance(ctype, DateTime):
        return datetime.fromisoformat(value) if isinstance(value, str) else value
    if isinstance(ctype, Numeric):
        return Decimal(str(value))
    if isinstance(ctype, Boolean):
        return bool(value)
    if column_name == "public_id" and isinstance(value, str):
        return uuid.UUID(value)
    if isinstance(value, (datetime, date)):
        return value
    return value


def _coerce_occ(column_name: str, value: object) -> object:
    """Convert a JSON-decoded value back to a PriceObservationOccurrence column's Python type."""
    if value is None:
        return None
    ctype = PriceObservationOccurrence.__table__.columns[column_name].type
    if isinstance(ctype, DateTime):
        return datetime.fromisoformat(value) if isinstance(value, str) else value
    if isinstance(ctype, Numeric):
        return Decimal(str(value))
    if isinstance(ctype, Boolean):
        return bool(value)
    if column_name == "public_id" and isinstance(value, str):
        return uuid.UUID(value)
    return value


def restore_manifest(db: Session, manifest_id: str, *, commit: bool) -> dict[str, Any]:
    """EXACT restore: reconstruct each removed row with its ORIGINAL id, and verify the restored row
    hash equals the original hash. ``commit=False`` proves it inside a rolled-back txn."""
    from cestaplan_api.models import AuditLog

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
    hash_ok = 0
    occ_relinked_back = 0
    occ_recreated = 0
    obs_cols = PriceObservation.__table__.columns
    occ_cols = PriceObservationOccurrence.__table__.columns
    for r in to_restore:
        values = {k: _coerce(k, v) for k, v in r["values"].items() if k in obs_cols}
        obs = PriceObservation(**values)  # exact restore: original id preserved
        db.add(obs)
        db.flush()
        if ident.row_hash(ident.row_values(obs)) == r["row_hash"]:
            hash_ok += 1
        restored += 1
        # Move relinked occurrences back onto the restored row.
        for occ in r.get("occurrences_relinked", []):
            db.execute(
                update(PriceObservationOccurrence)
                .where(PriceObservationOccurrence.id == occ["id"])
                .values(price_observation_id=occ["original_observation_id"])
            )
            occ_relinked_back += 1
        # Recreate occurrences that were dropped as duplicates, with their original id.
        for occ in r.get("occurrences_dropped", []):
            ovals = {k: _coerce_occ(k, v) for k, v in occ["values"].items() if k in occ_cols}
            db.add(PriceObservationOccurrence(**ovals))
            occ_recreated += 1
    db.flush()
    result = {
        "manifest_id": manifest_id,
        "restore_type": "exact_restore",
        "restorable_rows": len(to_restore),
        "reconstructed": restored,
        "hash_matches": hash_ok,
        "occurrences_relinked_back": occ_relinked_back,
        "occurrences_recreated": occ_recreated,
    }
    db.commit() if commit else db.rollback()
    return result


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--provider", default=None)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--apply", action="store_true")
    p.add_argument("--expected-delete-count", type=int, default=None)
    p.add_argument("--manifest-path", default=None)
    p.add_argument("--restore-manifest", default=None)
    p.add_argument("--commit-restore", action="store_true")
    a = p.parse_args(argv)

    with SessionLocal() as db:
        if a.restore_manifest:
            out = restore_manifest(db, a.restore_manifest, commit=a.commit_restore)
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
            out = dry_run(db, a.provider)
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
