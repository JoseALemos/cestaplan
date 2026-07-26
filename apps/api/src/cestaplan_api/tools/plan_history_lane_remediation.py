"""READ-ONLY, deterministic PLANNER for remediating legacy history-lane anomalies (design phase).

It NEVER writes: it only SELECTs (under a REPEATABLE READ, READ ONLY snapshot), classifies each
lane's rows, and PROPOSES a reversible plan WITHOUT deleting any fact or evidence. ``--apply`` is
rejected. This is a PLAN-ONLY tool: ``apply_ready`` is ALWAYS False.

Safety of the portable manifest (spec §1): it never contains a URL, secret, payload, header, token,
credential or connection string. ``source_url`` (and anything that looks like a URL/secret) is
redacted to ``*_present`` + ``*_hash`` — the value is hashed in memory, never emitted — and a
recursive output scanner fails the run if anything sensitive slips through. Each row is split into
immutable identity, original temporal state, and integrity (a ``full_row_hash`` over ALL fields,
including the redacted ones, so a future apply can verify the live row is unchanged without ever
seeing the sensitive value).

The plan proposes only temporal-state changes (valid_from/valid_until/verification_status/
rolled_back_at/rolled_back_by/closed_by_run_id) plus a proposed ``create_price_anomaly`` side
effect; it never touches a fact-identity field or original provenance and assigns no database ids.
The global
``plan_hash`` seals every relevant input, including the FULL content of every side effect, policy,
prerequisite and exclusion.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from sqlalchemy import MetaData, Table, func, inspect, select, text
from sqlalchemy.orm import Session

from cestaplan_api.db import Base, SessionLocal
from cestaplan_api.ingestion.providers.onboarding import get_entry
from cestaplan_api.models import (
    PriceAnomaly,
    PriceObservation,
    PriceObservationOccurrence,
    PromotionRule,
    Retailer,
)
from cestaplan_api.services import observation_identity as ident
from cestaplan_api.services.price_history_lane import lane_invariant_report

SCHEMA_VERSION = 3
TOOL_VERSION = "0.3.0-plan-only"

MUTABLE_STATE_FIELDS = (
    "valid_from", "valid_until", "verification_status",
    "rolled_back_at", "rolled_back_by", "closed_by_run_id",
)

_ROLLBACK_MARKER = "<remediation_run_ts>"
_DISPUTED = "disputed"
_SAME_TIMESTAMP_CONFLICT = "same_timestamp_conflict"
_REDACTED = "<redacted>"

# Fields whose VALUE must never appear in the manifest (only *_present + *_hash).
_SENSITIVE_FIELD_NAMES = frozenset({"source_url"})
_URL_RE = re.compile(r"https?://", re.IGNORECASE)
_SECRET_RE = re.compile(
    r"(api[_-]?key|secret|passwd|password|bearer|authorization|"
    r"postgres(?:ql)?://|mysql://|mongodb://|amqp://|redis://|database_url)",
    re.IGNORECASE,
)

# Incoming-FK handler registry (preserve/restore policy). The set of ACTUAL references is DISCOVERED
# from the live schema (spec §2/§5); this is only keyed by table name.
_FK_HANDLERS: dict[str, dict[str, Any]] = {
    "promotion_rule": {
        "apply_policy": "preserve_unchanged", "restore_policy": "preserve_unchanged"},
    "price_anomaly": {
        "apply_policy": "preserve_unchanged", "restore_policy": "preserve_unchanged"},
}
_OCCURRENCE_FK_TABLE = "price_observation_occurrence"
_HANDLED_FK_TABLES = set(_FK_HANDLERS) | {_OCCURRENCE_FK_TABLE}

_OCC_KEEP = (
    "id", "price_observation_id", "provider_code", "source_id", "crawl_run_id", "raw_capture_id",
    "connector_version", "parser_version", "imported_at", "confidence_score", "verification_status",
    "evidence_fingerprint",
)

_STATUS_RANK = {"human_verified": 3, "machine_verified": 2, "unverified": 1, "disputed": 0}
_PROV_FIELDS = ("provider_code", "source_id", "crawl_run_id", "raw_capture_id")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


# --------------------------------------------------------------------------- #
# Provenance (spec §6/§7) — never invented, never source-inspected.
# --------------------------------------------------------------------------- #
def _commit_provenance() -> dict[str, str]:
    return {
        "planner_commit_sha": os.environ.get("PLANNER_COMMIT_SHA", "unknown"),
        "database_code_sha": (
            os.environ.get("DATABASE_CODE_SHA")
            or os.environ.get("RAILWAY_GIT_COMMIT_SHA") or "unknown"),
        "base_main_sha": os.environ.get("BASE_MAIN_SHA", "unknown"),
    }


def _provenance_complete(prov: dict[str, str]) -> bool:
    # A short or malformed SHA is NOT accepted as exact provenance (spec §7).
    return all(_SHA_RE.match(v or "") for v in prov.values())


def _planner_source_hash() -> str:
    """SHA-256 over the exact bytes of the planner file that produced the manifest (spec §7)."""
    try:
        return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    except (OSError, NameError):
        return os.environ.get("PLANNER_SOURCE_HASH", "unknown")


# Plan-only: apply is NEVER ready here. These blockers are static, not inferred from writer source.
def _apply_blockers(prov_complete: bool) -> list[str]:
    blockers = ["planner_is_plan_only", "record_price_fact_rolled_back_reuse_not_remediated"]
    if not prov_complete:
        blockers.append("unknown_commit_provenance")
    return blockers


# --------------------------------------------------------------------------- #
# Read-only REPEATABLE READ snapshot (spec §2)
# --------------------------------------------------------------------------- #
def readonly_preflight(db: Session) -> dict[str, Any]:
    """Assert a clean PostgreSQL session and pin a REPEATABLE READ, READ ONLY snapshot BEFORE any
    read. Must be the first statement of the transaction."""
    assert not db.new and not db.dirty and not db.deleted, "planner requires a clean session"
    assert db.bind is not None and db.bind.dialect.name == "postgresql", "requires PostgreSQL"
    db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
    read_only = db.execute(text("SHOW transaction_read_only")).scalar() == "on"
    isolation = db.execute(text("SHOW transaction_isolation")).scalar()
    assert read_only and isolation == "repeatable read", "read-only snapshot not established"
    return {"transaction_read_only": read_only, "snapshot_isolation": isolation}


# --------------------------------------------------------------------------- #
# Sensitive-data redaction + output scanner (spec §1)
# --------------------------------------------------------------------------- #
def _scrub(v: Any) -> Any:
    """Recursively replace any URL/secret-looking string with a redaction marker."""
    if isinstance(v, str):
        return _REDACTED if (_URL_RE.search(v) or _SECRET_RE.search(v)) else v
    if isinstance(v, dict):
        return {k: _scrub(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_scrub(x) for x in v]
    return v


def _value_hash(v: Any) -> str:
    return hashlib.sha256(json.dumps(v, sort_keys=True, default=str).encode()).hexdigest()


def scan_sensitive(obj: Any, path: str = "$") -> list[str]:
    """Return violation paths if the (already-serialized) output still holds a URL/secret/raw
    sensitive field (spec §1). Empty list == clean."""
    hits: list[str] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in _SENSITIVE_FIELD_NAMES and isinstance(v, str) and v not in (_REDACTED, ""):
                hits.append(f"{path}.{k}")
            hits += scan_sensitive(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            hits += scan_sensitive(v, f"{path}[{i}]")
    elif isinstance(obj, str) and obj != _REDACTED and (
            _URL_RE.search(obj) or _SECRET_RE.search(obj)):
        hits.append(path)
    return hits


# --------------------------------------------------------------------------- #
# FK discovery — schema-safe + composite-safe (spec §5)
# --------------------------------------------------------------------------- #
_SYSTEM_SCHEMAS = {"information_schema", "pg_catalog", "pg_toast"}


def discover_incoming_fks(db: Session) -> list[dict[str, str]]:
    """Every FK column (any user schema) that references price_observation.id, via Inspector.
    Composite-safe: only the column paired with ``id`` is recorded."""
    insp = inspect(db.connection())
    found: list[dict[str, str]] = []
    schemas = [s for s in insp.get_schema_names() if s not in _SYSTEM_SCHEMAS]
    for schema in schemas:
        for table in insp.get_table_names(schema=schema):
            for fk in insp.get_foreign_keys(table, schema=schema):
                if fk.get("referred_table") != "price_observation":
                    continue
                for con_col, ref_col in zip(
                    fk["constrained_columns"], fk.get("referred_columns") or [], strict=False
                ):
                    if ref_col == "id":
                        found.append({
                            "schema": fk.get("referred_schema") or schema, "table": table,
                            "column": con_col, "supported": str(table in _HANDLED_FK_TABLES),
                        })
    return found


def metadata_fk_tables() -> set[str]:
    out: set[str] = set()
    for table in Base.metadata.tables.values():
        for fk in table.foreign_keys:
            if fk.column.table.name == "price_observation":
                out.add(table.name)
    return out


def _unknown_fk_refs(db: Session, discovered, obs_ids) -> dict[int, list[str]]:
    """Which observations are referenced by an UNKNOWN FK (excludes their lane). Uses reflection +
    SQLAlchemy constructors — never f-string identifiers (spec §5)."""
    refs: dict[int, list[str]] = defaultdict(list)
    if not obs_ids:
        return refs
    for fk in discovered:
        if fk["table"] in _HANDLED_FK_TABLES:
            continue
        ref = f"{fk['schema']}.{fk['table']}.{fk['column']}"
        t = Table(fk["table"], MetaData(), schema=fk["schema"], autoload_with=db.connection())
        for (oid,) in db.execute(
            select(t.c[fk["column"]]).where(t.c[fk["column"]].in_(obs_ids))
        ).all():
            if oid is not None:
                refs[oid].append(ref)
    return refs


# --------------------------------------------------------------------------- #
# Load
# --------------------------------------------------------------------------- #
def _retailer_id(db: Session, provider_code: str) -> int | None:
    entry = get_entry(provider_code)
    slug = entry.retailer_slug if entry else provider_code
    return db.scalar(select(Retailer.id).where(Retailer.slug == slug))


def _occ_manifest(o: PriceObservationOccurrence) -> dict[str, Any]:
    full = {c.name: json.loads(json.dumps(getattr(o, c.name), default=str))
            for c in PriceObservationOccurrence.__table__.columns}
    keep: dict[str, Any] = {k: _scrub(full.get(k)) for k in _OCC_KEEP}
    keep["occurrence_hash"] = ident.row_hash(full)
    keep["source_url_present"] = full.get("source_url") is not None
    keep["source_url_hash"] = _value_hash(full["source_url"]) if full.get("source_url") else None
    return keep


def _fk_manifest(table: str, model, row) -> dict[str, Any]:
    full = {c.name: json.loads(json.dumps(getattr(row, c.name), default=str))
            for c in model.__table__.columns}
    handler = _FK_HANDLERS[table]
    sanitized = {k: (None if k in _SENSITIVE_FIELD_NAMES else _scrub(v)) for k, v in full.items()}
    entry = {
        "schema": "public", "table": table, "pk": row.id, "fk_column": "price_observation_id",
        "sanitized_values": sanitized, "full_row_hash": ident.row_hash(full),
        "apply_policy": handler["apply_policy"], "restore_policy": handler["restore_policy"],
        "kind": "preexisting",
    }
    if "source_url" in full:
        entry["source_url_present"] = full["source_url"] is not None
        entry["source_url_hash"] = _value_hash(full["source_url"]) if full["source_url"] else None
    return entry


def _load(db: Session, provider_code: str | None):
    stmt = select(PriceObservation).where(
        PriceObservation.rolled_back_at.is_(None), PriceObservation.staging_only.is_(True))
    retailer_id = None
    if provider_code is not None:
        retailer_id = _retailer_id(db, provider_code)
        if retailer_id is None:
            return {}, {}, {}, {}, None, []
        stmt = stmt.where(PriceObservation.retailer_id == retailer_id)
    rows = list(db.execute(stmt).scalars())
    obs_ids = [r.id for r in rows]

    occ_by_obs: dict[int, list[PriceObservationOccurrence]] = defaultdict(list)
    if obs_ids:
        for occ in db.execute(select(PriceObservationOccurrence).where(
                PriceObservationOccurrence.price_observation_id.in_(obs_ids))).scalars():
            occ_by_obs[occ.price_observation_id].append(occ)

    discovered = discover_incoming_fks(db)
    supported_fk: dict[int, list[dict[str, Any]]] = defaultdict(list)
    _models = {"promotion_rule": PromotionRule, "price_anomaly": PriceAnomaly}
    if obs_ids:
        for table, model in _models.items():
            for row in db.execute(
                select(model).where(model.price_observation_id.in_(obs_ids))).scalars():
                supported_fk[row.price_observation_id].append(_fk_manifest(table, model, row))
    unknown_fk = _unknown_fk_refs(db, discovered, obs_ids)

    lanes: dict[str, list[PriceObservation]] = defaultdict(list)
    for r in rows:
        lanes[ident.price_history_lane_fingerprint(r)].append(r)
    return (dict(lanes), dict(occ_by_obs), dict(supported_fk), dict(unknown_fk),
            retailer_id, discovered)


# --------------------------------------------------------------------------- #
# Canonical policy
# --------------------------------------------------------------------------- #
def _prov_completeness(occs) -> int:
    return max((sum(1 for f in _PROV_FIELDS if getattr(o, f) is not None) for o in occs), default=0)


def _verifiable_capture(row, occs) -> bool:
    if row.crawl_run_id is not None or row.raw_capture_id is not None:
        return True
    return any(o.crawl_run_id is not None or o.raw_capture_id is not None for o in occs)


def _canonical_key(row, occ_by_obs) -> tuple:
    occs = occ_by_obs.get(row.id, [])
    verif = _STATUS_RANK.get(row.verification_status or "", 0) * 1000 + int(
        (row.confidence_score or 0) * 100)
    return (-len(occs), -_prov_completeness(occs), -verif,
            -int(_verifiable_capture(row, occs)), row.imported_at, row.id)


# --------------------------------------------------------------------------- #
# Plan one lane
# --------------------------------------------------------------------------- #
def _sim_from(row) -> SimpleNamespace:
    attrs = {f: getattr(row, f) for f in ident.LANE_FIELDS}
    attrs.update(id=row.id, observed_at=row.observed_at, valid_from=row.valid_from,
                 valid_until=row.valid_until, verification_status=row.verification_status)
    return SimpleNamespace(**attrs)


def _original_temporal(row) -> dict[str, Any]:
    full = ident.row_values(row)
    return {f: full[f] for f in MUTABLE_STATE_FIELDS}


def _split_row(row) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    full = ident.row_values(row)
    identity: dict[str, Any] = {}
    integrity: dict[str, Any] = {"full_row_hash": ident.row_hash(full)}
    for k, v in full.items():
        if k in MUTABLE_STATE_FIELDS:
            continue
        if k in _SENSITIVE_FIELD_NAMES:
            integrity[f"{k}_present"] = v is not None
            integrity[f"{k}_hash"] = _value_hash(v) if v is not None else None
            continue
        identity[k] = _scrub(v)
    return identity, {f: full[f] for f in MUTABLE_STATE_FIELDS}, integrity


def _template_hash(row, expected: dict[str, Any]) -> str:
    full = ident.row_values(row)
    tmpl = dict(full)
    for k, v in expected.items():
        tmpl[k] = v if v == _ROLLBACK_MARKER else json.loads(
            json.dumps(v, default=ident._json_default))
    if expected.get("rolled_back_at") == _ROLLBACK_MARKER:
        tmpl["rolled_back_at"] = _ROLLBACK_MARKER
    return ident.row_hash(tmpl)


def _excluded_lane(lane_fp, rows, occ_by_obs, supported_fk, reasons) -> dict[str, Any]:
    """An excluded lane carries ZERO executable changes (spec §4): every row reverts to its ORIGINAL
    state, its template hash IS the original full-row hash, and it keeps full evidence."""
    manifest_rows = []
    for r in rows:
        identity, temporal, integrity = _split_row(r)
        manifest_rows.append({
            "id": r.id, "fact_fingerprint": ident.price_fact_fingerprint(r),
            "classification": "excluded", "diagnostic_classification": "excluded",
            "action": "excluded_no_action",
            "immutable_identity": identity, "original_temporal_state": temporal,
            "integrity": integrity,
            "expected_state_template": temporal,
            "expected_template_hash": integrity["full_row_hash"],
            "occurrences": [_occ_manifest(o) for o in occ_by_obs.get(r.id, [])],
            "incoming_fk_state": supported_fk.get(r.id, []),
        })
    return {
        "lane_fingerprint": lane_fp, "excluded": True, "apply_allowed": False,
        "exclusion_reasons": sorted(set(reasons)),
        "exact_duplicate_groups": 0, "exact_duplicate_rows": 0, "semantic_conflict_groups": 0,
        "semantic_conflict_representatives": 0, "facts_to_logically_rollback": 0,
        "facts_to_mark_disputed": 0, "intervals_to_reconstruct": 0,
        "occurrences_in_lane": sum(len(occ_by_obs.get(r.id, [])) for r in rows),
        "rows": manifest_rows, "proposed_actions": [], "proposed_side_effects": [],
        "projected_invariants": {}, "planned_changes": 0,
    }


def _plan_lane(lane_fp, rows, occ_by_obs, supported_fk, unknown_fk) -> dict[str, Any]:
    # Early exclusion (before any timestamp arithmetic): a null anchor or an unknown incoming FK.
    early: list[str] = []
    if any(r.observed_at is None or r.valid_from is None for r in rows):
        early.append("null_timestamp")
    for r in rows:
        for ref in unknown_fk.get(r.id, []):
            early.append(f"uncovered_fk:{ref}")
    if early:
        return _excluded_lane(lane_fp, rows, occ_by_obs, supported_fk, early)

    reasons: list[str] = []
    fp = {r.id: ident.price_fact_fingerprint(r) for r in rows}
    by_fp: dict[str, list] = defaultdict(list)
    for r in rows:
        by_fp[fp[r.id]].append(r)

    diagnostic: dict[int, str] = {}
    actions: dict[int, str] = {}
    canonical_of_fp: dict[str, Any] = {}
    exact_groups = 0
    for f, group in by_fp.items():
        if len(group) > 1:
            exact_groups += 1
        canonical = min(group, key=lambda r: _canonical_key(r, occ_by_obs))
        canonical_of_fp[f] = canonical
        for r in group:
            if r.id != canonical.id:
                actions[r.id] = "logical_rollback_exact_duplicate"
                diagnostic[r.id] = "exact_duplicate_noncanonical"

    canon_by_ts: dict[Any, list] = defaultdict(list)
    for canonical in canonical_of_fp.values():
        canon_by_ts[canonical.observed_at].append(canonical)
    conflict_ts = {t for t, reps in canon_by_ts.items() if len(reps) > 1}
    for t in conflict_ts:
        for rep in canon_by_ts[t]:
            actions[rep.id] = "mark_disputed_same_timestamp_conflict"
            diagnostic[rep.id] = "same_timestamp_semantic_conflict_representative"

    if any(r.verification_status == "human_verified"
           and actions.get(r.id) == "mark_disputed_same_timestamp_conflict" for r in rows):
        reasons.append("human_reviewed_conflict")

    active = [r for r in canonical_of_fp.values()
              if actions.get(r.id) != "mark_disputed_same_timestamp_conflict"]
    disputed_reps = [r for r in rows
                     if actions.get(r.id) == "mark_disputed_same_timestamp_conflict"]
    anchors = sorted({r.observed_at for r in active} | {r.observed_at for r in disputed_reps})

    def next_anchor(t):
        after = [a for a in anchors if a > t]
        return after[0] if after else None

    reconstruct = 0
    expected: dict[int, dict[str, Any]] = {}
    for r in active:
        want_until = next_anchor(r.observed_at)
        if r.id not in actions:
            if r.valid_from != r.observed_at or r.valid_until != want_until:
                actions[r.id] = "reconstruct_interval"
                diagnostic[r.id] = "sequential_unique"
                reconstruct += 1
            else:
                actions[r.id] = "keep"
                diagnostic[r.id] = "sequential_unique_or_canonical"
        expected[r.id] = {"valid_from": r.observed_at, "valid_until": want_until,
                          "verification_status": r.verification_status, "rolled_back_at": None}
    for r in rows:
        act = actions.get(r.id, "keep")
        diagnostic.setdefault(r.id, "sequential_unique_or_canonical")
        if act == "mark_disputed_same_timestamp_conflict":
            expected[r.id] = {"valid_from": r.observed_at, "valid_until": r.observed_at,
                              "verification_status": _DISPUTED, "rolled_back_at": None}
        elif act == "logical_rollback_exact_duplicate":
            t = _original_temporal(r)
            expected[r.id] = {**t, "rolled_back_at": _ROLLBACK_MARKER}
        elif r.id not in expected:
            expected[r.id] = _original_temporal(r)
        actions[r.id] = act

    sim_ok, sim_report = _simulate(rows, actions, expected)
    if not sim_ok:
        reasons.append("post_sim_invariant_fail")
    if reasons:  # late exclusion (human-reviewed conflict / failed sim) -> no residual actions
        return _excluded_lane(lane_fp, rows, occ_by_obs, supported_fk, reasons)

    side_effects: list[dict[str, Any]] = []
    for r in disputed_reps:
        target = _split_row(r)[2]["full_row_hash"]
        se = {
            "type": "create_price_anomaly", "anomaly_type": _SAME_TIMESTAMP_CONFLICT,
            "severity": "high", "target_observation_ref": target,
            "original_state": "absent", "restore_action": "delete_only_created_row",
            "expected_payload_template": {
                "anomaly_type": _SAME_TIMESTAMP_CONFLICT, "severity": "high",
                "price_observation_ref": target, "status": "open"},
        }
        se["deterministic_action_id"] = _value_hash(
            {"lane": lane_fp, **{k: se[k] for k in (
                "type", "anomaly_type", "severity", "target_observation_ref",
                "restore_action", "expected_payload_template")}})
        side_effects.append(se)

    manifest_rows = []
    for r in rows:
        identity, temporal, integrity = _split_row(r)
        manifest_rows.append({
            "id": r.id, "fact_fingerprint": fp[r.id],
            "classification": diagnostic[r.id], "diagnostic_classification": diagnostic[r.id],
            "action": actions[r.id],
            "immutable_identity": identity, "original_temporal_state": temporal,
            "integrity": integrity,
            "expected_state_template": expected[r.id],
            "expected_template_hash": _template_hash(r, expected[r.id]),
            "occurrences": [_occ_manifest(o) for o in occ_by_obs.get(r.id, [])],
            "incoming_fk_state": supported_fk.get(r.id, []),
        })

    return {
        "lane_fingerprint": lane_fp, "excluded": False, "apply_allowed": True,
        "exclusion_reasons": [],
        "exact_duplicate_groups": exact_groups,
        "exact_duplicate_rows": sum(
            1 for a in actions.values() if a == "logical_rollback_exact_duplicate"),
        "semantic_conflict_groups": len(conflict_ts),
        "semantic_conflict_representatives": len(disputed_reps),
        "facts_to_logically_rollback": sum(
            1 for a in actions.values() if a == "logical_rollback_exact_duplicate"),
        "facts_to_mark_disputed": len(disputed_reps),
        "intervals_to_reconstruct": reconstruct,
        "occurrences_in_lane": sum(len(occ_by_obs.get(r.id, [])) for r in rows),
        "rows": manifest_rows,
        "proposed_actions": sorted({a for a in actions.values() if a != "keep"}),
        "proposed_side_effects": side_effects,
        "projected_invariants": sim_report,
        "planned_changes": sum(1 for a in actions.values() if a != "keep"),
    }


def _simulate(rows, actions, expected) -> tuple[bool, dict[str, Any]]:
    sims = []
    for r in rows:
        exp = expected[r.id]
        if exp.get("rolled_back_at") is not None:
            continue
        s = _sim_from(r)
        s.valid_from, s.valid_until = exp["valid_from"], exp["valid_until"]
        s.verification_status = exp["verification_status"]
        sims.append(s)
    report = lane_invariant_report(sims)
    return _sim_report_ok(report), report


def _sim_report_ok(r) -> bool:
    return (r["lanes_multiple_open"] == 0 and r["lanes_overlapping_intervals"] == 0
            and r["lanes_repeated_timestamp"] == 0 and r["rows_non_positive_interval"] == 0
            and r["active_intervals_crossing_disputed"] == 0 and r["disputed_rows_non_empty"] == 0)


# --------------------------------------------------------------------------- #
# Dry-run
# --------------------------------------------------------------------------- #
def _ambiguous_rows(rows, occ_by_obs) -> int:
    n = 0
    for r in rows:
        occs = occ_by_obs.get(r.id, [])
        if not occs or all(all(getattr(o, f) is None for f in _PROV_FIELDS) for o in occs):
            n += 1
    return n


def dry_run(
    db: Session, provider_code: str | None = None, *, snapshot: bool = False
) -> dict[str, Any]:
    snap = readonly_preflight(db) if snapshot else {
        "transaction_read_only": None, "snapshot_isolation": None}
    lanes, occ_by_obs, supported_fk, unknown_fk, retailer_id, discovered = _load(db, provider_code)
    baseline = {
        "price_observation": int(
            db.scalar(select(func.count()).select_from(PriceObservation)) or 0),
        "price_observation_occurrence": int(
            db.scalar(select(func.count()).select_from(PriceObservationOccurrence)) or 0),
    }
    counts = dict.fromkeys((
        "lanes_scanned", "lanes_anomalous", "lanes_plannable", "lanes_excluded",
        "exact_duplicate_groups", "exact_duplicate_rows", "semantic_conflict_groups",
        "semantic_conflict_representatives", "facts_to_logically_rollback",
        "facts_to_mark_disputed",
        "intervals_to_reconstruct", "occurrences_scanned_total", "occurrences_in_planned_lanes",
        "ambiguous_provenance_scanned", "ambiguous_provenance_in_planned_lanes",
        "fk_dependencies_scanned", "fk_dependencies_in_planned_lanes", "manual_review_required",
    ), 0)
    counts["occurrences_scanned_total"] = sum(len(v) for v in occ_by_obs.values())
    exclusion_reasons: dict[str, int] = defaultdict(int)
    lane_plans: list[dict[str, Any]] = []

    for lane_fp, rows in sorted(lanes.items()):
        inv = lane_invariant_report(rows)
        anomalous = not _sim_report_ok(inv)
        counts["lanes_scanned"] += 1
        counts["ambiguous_provenance_scanned"] += _ambiguous_rows(rows, occ_by_obs)
        counts["fk_dependencies_scanned"] += sum(len(supported_fk.get(r.id, [])) for r in rows)
        if not anomalous:
            continue
        counts["lanes_anomalous"] += 1
        plan = _plan_lane(lane_fp, rows, occ_by_obs, supported_fk, unknown_fk)
        plan["anomalous"] = True
        lane_plans.append(plan)
        if plan["excluded"]:
            counts["lanes_excluded"] += 1
            for reason in plan["exclusion_reasons"]:
                exclusion_reasons[reason] += 1
        else:
            counts["lanes_plannable"] += 1
            for k in ("exact_duplicate_groups", "exact_duplicate_rows", "semantic_conflict_groups",
                      "semantic_conflict_representatives", "facts_to_logically_rollback",
                      "facts_to_mark_disputed", "intervals_to_reconstruct"):
                counts[k] += plan[k]
            counts["occurrences_in_planned_lanes"] += plan["occurrences_in_lane"]
            counts["ambiguous_provenance_in_planned_lanes"] += _ambiguous_rows(rows, occ_by_obs)
            counts["fk_dependencies_in_planned_lanes"] += sum(
                len(supported_fk.get(r.id, [])) for r in rows)

    counts["manual_review_required"] = (
        exclusion_reasons.get("human_reviewed_conflict", 0)
        + sum(v for k, v in exclusion_reasons.items() if k.startswith("uncovered_fk")))

    provenance = _commit_provenance()
    prov_complete = _provenance_complete(provenance)
    blockers = _apply_blockers(prov_complete)
    apply_prerequisites = [
        "record_price_fact excludes rolled_back_at IS NULL (separate writer PR + tests)",
        "writer_contract verified (version + commit + test evidence + deployed/main SHA)",
        "all commit provenance known (full 40-hex planner/database/base_main SHAs)",
    ]
    manifest = _manifest(provider_code, retailer_id, baseline, lane_plans, discovered, provenance,
                         prov_complete, blockers, apply_prerequisites, snap)

    report: dict[str, Any] = {k: int(v) for k, v in counts.items()}
    report["exclusion_reasons"] = dict(exclusion_reasons)
    report["projected_invariants_all_ok"] = all(
        _sim_report_ok(p["projected_invariants"]) for p in lane_plans if not p["excluded"])
    report["fk_discovered"] = sorted(
        f"{f['schema']}.{f['table']}.{f['column']}" for f in discovered)
    report["fk_supported"] = sorted(
        {f["table"] for f in discovered if f["table"] in _HANDLED_FK_TABLES})
    report["fk_unknown"] = sorted(
        {f["table"] for f in discovered if f["table"] not in _HANDLED_FK_TABLES})
    report["apply_ready"] = False
    report["apply_blockers"] = blockers
    report["writer_contract_status"] = "unverified"
    report.update(provenance)
    report["commit_provenance_complete"] = prov_complete
    report["planner_source_hash"] = manifest["planner_source_hash"]
    report["transaction_read_only"] = snap["transaction_read_only"]
    report["snapshot_isolation"] = snap["snapshot_isolation"]
    report["plan_hash"] = manifest["plan_hash"]
    report["output_sensitive_scan_passed"] = not scan_sensitive(manifest)
    return {"report": report, "manifest": manifest}


def _seal(provider_code, retailer_id, baseline, lane_plans, discovered, provenance, prov_complete,
          blockers, apply_prerequisites, planner_source_hash) -> str:
    lanes_seal = sorted((
        {
            "lane_fingerprint": p["lane_fingerprint"], "excluded": p["excluded"],
            "exclusion_reasons": sorted(p["exclusion_reasons"]),
            "rows": sorted((
                {
                    "full_row_hash": r["integrity"]["full_row_hash"], "action": r["action"],
                    "classification": r["classification"],
                    "expected_state_template": _canon(r["expected_state_template"]),
                    "expected_template_hash": r["expected_template_hash"],
                    "occurrence_hashes": sorted(o["occurrence_hash"] for o in r["occurrences"]),
                    "fk": sorted((
                        {"full_row_hash": fk["full_row_hash"], "apply_policy": fk["apply_policy"],
                         "restore_policy": fk["restore_policy"]}
                        for fk in r["incoming_fk_state"]),
                        key=lambda x: x["full_row_hash"]),
                } for r in p["rows"]),
                key=lambda x: x["full_row_hash"]),
            # FULL side-effect content is sealed (spec §3), not just the deterministic_action_id.
            "proposed_side_effects": sorted((_canon(se) for se in p["proposed_side_effects"]),
                                            key=lambda x: x["deterministic_action_id"]),
        } for p in lane_plans), key=lambda x: x["lane_fingerprint"])
    seal = {
        "schema_version": SCHEMA_VERSION, "tool_version": TOOL_VERSION,
        "planner_source_hash": planner_source_hash,
        "commit_provenance": {**provenance, "complete": prov_complete},
        "provider_code": provider_code, "retailer_id": retailer_id, "baseline_counts": baseline,
        "fk_discovered": sorted(
            f"{f['schema']}.{f['table']}.{f['column']}:{f['supported']}" for f in discovered),
        "apply_ready": False, "apply_blockers": sorted(blockers),
        "apply_prerequisites": sorted(apply_prerequisites),
        "lanes": lanes_seal,
    }
    return ident.row_hash(seal)


def _canon(d: dict[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(d, sort_keys=True, default=str))


def _manifest(provider_code, retailer_id, baseline, lane_plans, discovered, provenance,
              prov_complete, blockers, apply_prerequisites, snap) -> dict[str, Any]:
    psh = _planner_source_hash()
    plan_hash = _seal(provider_code, retailer_id, baseline, lane_plans, discovered, provenance,
                      prov_complete, blockers, apply_prerequisites, psh)
    return {
        "schema_version": SCHEMA_VERSION, "tool_version": TOOL_VERSION,
        "planner_source_hash": psh,
        "commit_provenance": {**provenance, "complete": prov_complete},
        "writer_contract_status": "unverified",
        "snapshot": snap,
        "generated_at": datetime.now(UTC).isoformat(),
        "provider_code": provider_code, "retailer_id": retailer_id, "baseline_counts": baseline,
        "fk_discovered": discovered,
        "apply_ready": False, "apply_blockers": blockers,
        "apply_prerequisites": apply_prerequisites,
        "plan_hash": plan_hash, "lanes": lane_plans,
    }


def _run(db, provider, snapshot) -> dict[str, Any]:
    result = dry_run(db, provider, snapshot=snapshot)
    if scan_sensitive(result["manifest"]):
        raise SystemExit("ABORT: sensitive data detected in manifest output.")
    return result


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--provider", default=None)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--apply", action="store_true")
    p.add_argument("--manifest-path", default=None)
    a = p.parse_args(argv)
    if a.apply:
        raise SystemExit(
            "ABORT: --apply is not implemented or authorized. This tool only produces a read-only "
            "plan (--dry-run). A separate, reviewed apply tool will consume the manifest.")
    with SessionLocal() as db:
        result = _run(db, a.provider, snapshot=True)
        db.rollback()
    if a.manifest_path:
        fd = os.open(a.manifest_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(result["manifest"], f, indent=2, default=str)
    json.dump(result["report"], sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
