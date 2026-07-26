"""READ-ONLY, deterministic PLANNER for remediating legacy history-lane anomalies (design phase).

It NEVER writes: it only SELECTs, classifies each lane's rows, and PROPOSES a reversible plan to
reconstruct a coherent interval chain WITHOUT deleting any fact or evidence. ``--apply`` is rejected
explicitly. The plan only ever proposes changes to TEMPORAL-STATE fields
(valid_from/valid_until/verification_status/rolled_back_at/rolled_back_by/closed_by_run_id) plus a
proposed ``create_price_anomaly`` side effect; it never touches a fact-identity field or original
provenance, and it does not assign database ids.

Classification order per timestamp (spec §1): group by full ``price_fact_fingerprint`` FIRST; pick
one canonical per fingerprint (the rest -> ``logical_rollback_exact_duplicate``, never overwritten);
a same-timestamp SEMANTIC conflict is computed ONLY between the CANONICAL representatives of
distinct fingerprints, and only those canonical reps become ``disputed``.

The dry-run emits a versioned reversible manifest (all lanes — planned AND excluded — with full
original row state, occurrences with hashes, discovered incoming FK with full state + apply/restore
policy, per-row classification, proposed actions, expected-state TEMPLATES + template hashes,
proposed side effects, exclusions, apply prerequisites) and a sanitized counts report. The global
``plan_hash`` seals every relevant input. No secrets, URLs or raw payloads.

``apply_ready`` is reported but is expected to be False here: the plan is *plannable* but not
*apply-ready* until a separate writer PR makes ``_find_existing_fact`` exclude rolled-back rows and
the commit provenance is fully known.
"""

from __future__ import annotations

import argparse
import inspect as _inspect
import json
import os
import sys
from collections import defaultdict
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from sqlalchemy import func, inspect, select, text
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
from cestaplan_api.services import observation_persistence as _op
from cestaplan_api.services.price_history_lane import lane_invariant_report

SCHEMA_VERSION = 2
TOOL_VERSION = "0.2.0-plan-only"

# Only these temporal-STATE fields may ever be proposed for change (spec §4/§5). Everything else —
# every fact-identity field and all original provenance — is immutable.
MUTABLE_STATE_FIELDS = (
    "valid_from", "valid_until", "verification_status",
    "rolled_back_at", "rolled_back_by", "closed_by_run_id",
)

_ROLLBACK_MARKER = "<remediation_run_ts>"
_DISPUTED = "disputed"
_SAME_TIMESTAMP_CONFLICT = "same_timestamp_conflict"

# Incoming-FK tables we know how to preserve/restore. The set of ACTUAL references is DISCOVERED
# the live schema (spec §2); this is only the handler registry keyed by table name.
_FK_HANDLERS: dict[str, dict[str, Any]] = {
    "promotion_rule": {
        "model": PromotionRule, "fk_column": "price_observation_id",
        "apply_policy": "preserve_unchanged", "restore_policy": "preserve_unchanged",
    },
    "price_anomaly": {
        "model": PriceAnomaly, "fk_column": "price_observation_id",
        "apply_policy": "preserve_unchanged", "restore_policy": "preserve_unchanged",
    },
}

# price_observation_occurrence also references price_observation.id, but it is handled specially
# (each row's occurrences are captured in the manifest and preserved) — NOT a blocking FK.
_OCCURRENCE_FK_TABLE = "price_observation_occurrence"
_HANDLED_FK_TABLES = set(_FK_HANDLERS) | {_OCCURRENCE_FK_TABLE}

_STATUS_RANK = {"human_verified": 3, "machine_verified": 2, "unverified": 1, "disputed": 0}
_PROV_FIELDS = ("provider_code", "source_id", "crawl_run_id", "raw_capture_id")


# --------------------------------------------------------------------------- #
# Commit provenance (spec §6) — never invented.
# --------------------------------------------------------------------------- #
def _commit_provenance() -> dict[str, str]:
    return {
        "planner_commit_sha": os.environ.get("PLANNER_COMMIT_SHA", "unknown"),
        "database_code_sha": (
            os.environ.get("DATABASE_CODE_SHA")
            or os.environ.get("RAILWAY_GIT_COMMIT_SHA")
            or "unknown"
        ),
        "base_main_sha": os.environ.get("BASE_MAIN_SHA", "unknown"),
    }


def _writer_reuse_blocker() -> bool:
    """True while record_price_fact could reuse a rolled-back exact duplicate (spec §7)."""
    src = _inspect.getsource(_op._find_existing_fact)
    return "rolled_back_at" not in src


# --------------------------------------------------------------------------- #
# FK discovery (spec §2) — from the live schema, never a hardcoded list.
# --------------------------------------------------------------------------- #
def discover_incoming_fks(db: Session) -> list[dict[str, str]]:
    """Every FK in the DB that references price_observation.id, discovered via Inspector."""
    insp = inspect(db.connection())
    found: list[dict[str, str]] = []
    for table in insp.get_table_names():
        for fk in insp.get_foreign_keys(table):
            if fk.get("referred_table") == "price_observation" and "id" in (
                fk.get("referred_columns") or []
            ):
                for col in fk["constrained_columns"]:
                    found.append(
                        {"schema": fk.get("referred_schema") or "public", "table": table,
                         "column": col, "supported": str(table in _HANDLED_FK_TABLES)}
                    )
    return found


def metadata_fk_tables() -> set[str]:
    """Tables whose ORM model declares an FK to price_observation (for the handler guard test)."""
    out: set[str] = set()
    for table in Base.metadata.tables.values():
        for fk in table.foreign_keys:
            if fk.column.table.name == "price_observation":
                out.add(table.name)
    return out


# --------------------------------------------------------------------------- #
# Load (read-only)
# --------------------------------------------------------------------------- #
def _retailer_id(db: Session, provider_code: str) -> int | None:
    entry = get_entry(provider_code)
    slug = entry.retailer_slug if entry else provider_code
    return db.scalar(select(Retailer.id).where(Retailer.slug == slug))


def _occ_values(o: PriceObservationOccurrence) -> dict[str, Any]:
    return {c.name: json.loads(json.dumps(getattr(o, c.name), default=str))
            for c in PriceObservationOccurrence.__table__.columns}


def _fk_row_state(model, row) -> dict[str, Any]:
    vals = {c.name: json.loads(json.dumps(getattr(row, c.name), default=str))
            for c in model.__table__.columns}
    return vals


def _load(db: Session, provider_code: str | None):
    stmt = select(PriceObservation).where(
        PriceObservation.rolled_back_at.is_(None), PriceObservation.staging_only.is_(True)
    )
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
        for occ in db.execute(
            select(PriceObservationOccurrence).where(
                PriceObservationOccurrence.price_observation_id.in_(obs_ids))
        ).scalars():
            occ_by_obs[occ.price_observation_id].append(occ)

    discovered = discover_incoming_fks(db)
    # Supported FK rows (full state) keyed by observation id.
    supported_fk: dict[int, list[dict[str, Any]]] = defaultdict(list)
    if obs_ids:
        for table, handler in _FK_HANDLERS.items():
            model = handler["model"]
            col = getattr(model, handler["fk_column"])
            for row in db.execute(select(model).where(col.in_(obs_ids))).scalars():
                oid = getattr(row, handler["fk_column"])
                supported_fk[oid].append({
                    "schema": "public", "table": table,
                    "pk": row.id, "fk_column": handler["fk_column"],
                    "original_values": _fk_row_state(model, row),
                    "original_hash": ident.row_hash(_fk_row_state(model, row)),
                    "apply_policy": handler["apply_policy"],
                    "restore_policy": handler["restore_policy"],
                    "kind": "preexisting",
                })
    # UNKNOWN FK references (from the live schema) that hit our observations -> exclusion triggers.
    unknown_fk: dict[int, list[str]] = defaultdict(list)
    for fk in discovered:
        if fk["table"] in _HANDLED_FK_TABLES or not obs_ids:
            continue
        ref = f"{fk['schema']}.{fk['table']}.{fk['column']}"
        for (oid,) in db.execute(
            text(f'SELECT "{fk["column"]}" FROM "{fk["table"]}" '
                 f'WHERE "{fk["column"]}" = ANY(:ids)'),
            {"ids": obs_ids},
        ).all():
            if oid is not None:
                unknown_fk[oid].append(ref)

    lanes: dict[str, list[PriceObservation]] = defaultdict(list)
    for r in rows:
        lanes[ident.price_history_lane_fingerprint(r)].append(r)
    return (dict(lanes), dict(occ_by_obs), dict(supported_fk), dict(unknown_fk),
            retailer_id, discovered)


# --------------------------------------------------------------------------- #
# Canonical policy (stable, deterministic)
# --------------------------------------------------------------------------- #
def _prov_completeness(occs) -> int:
    best = 0
    for o in occs:
        best = max(best, sum(1 for f in _PROV_FIELDS if getattr(o, f) is not None))
    return best


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


def _plan_lane(lane_fp, rows, occ_by_obs, supported_fk, unknown_fk) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "lane_fingerprint": lane_fp, "excluded": False, "exclusion_reasons": [],
    }
    if any(r.observed_at is None or r.valid_from is None for r in rows):
        entry["excluded"] = True
        entry["exclusion_reasons"].append("null_timestamp")
    for r in rows:
        for ref in unknown_fk.get(r.id, []):
            entry["excluded"] = True
            entry["exclusion_reasons"].append(f"uncovered_fk:{ref}")

    fp = {r.id: ident.price_fact_fingerprint(r) for r in rows}

    # 1) group by fingerprint; 2) one canonical per fingerprint; 3) non-canonical -> rollback.
    by_fp: dict[str, list] = defaultdict(list)
    for r in rows:
        by_fp[fp[r.id]].append(r)
    actions: dict[int, dict[str, Any]] = {}
    canonical_of_fp: dict[str, Any] = {}
    exact_groups = 0
    for f, group in by_fp.items():
        if len(group) > 1:
            exact_groups += 1
        canonical = min(group, key=lambda r: _canonical_key(r, occ_by_obs))
        canonical_of_fp[f] = canonical
        for r in group:
            if r.id != canonical.id:
                actions[r.id] = {"action": "logical_rollback_exact_duplicate"}

    # 4) semantic conflict computed ONLY among canonical representatives at a timestamp.
    canon_by_ts: dict[Any, list] = defaultdict(list)
    for canonical in canonical_of_fp.values():
        canon_by_ts[canonical.observed_at].append(canonical)
    conflict_ts = {t for t, reps in canon_by_ts.items() if len(reps) > 1}
    # 5) only those canonical reps become disputed (never overwriting a rollback).
    for t in conflict_ts:
        for rep in canon_by_ts[t]:
            actions[rep.id] = {"action": "mark_disputed_same_timestamp_conflict"}

    # Gate: a human-reviewed CANONICAL rep inside a conflict needs manual review.
    if any(
        r.verification_status == "human_verified"
        and actions.get(r.id, {}).get("action") == "mark_disputed_same_timestamp_conflict"
        for r in rows
    ):
        entry["excluded"] = True
        entry["exclusion_reasons"].append("human_reviewed_conflict")

    # Active-after-plan = canonical reps that are NOT disputed.
    active = [
        r for r in canonical_of_fp.values()
        if actions.get(r.id, {}).get("action") != "mark_disputed_same_timestamp_conflict"
    ]
    disputed_reps = [
        r for r in rows
        if actions.get(r.id, {}).get("action") == "mark_disputed_same_timestamp_conflict"
    ]
    anchors = sorted({r.observed_at for r in active} | {r.observed_at for r in disputed_reps})

    def next_anchor(t):
        after = [a for a in anchors if a > t]
        return after[0] if after else None

    reconstruct = 0
    for r in active:
        want_until = next_anchor(r.observed_at)
        prev = actions.get(r.id)
        if prev is None:
            if r.valid_from != r.observed_at or r.valid_until != want_until:
                actions[r.id] = {"action": "reconstruct_interval"}
                reconstruct += 1
            else:
                actions[r.id] = {"action": "keep"}
        actions[r.id]["expected"] = {
            "valid_from": r.observed_at, "valid_until": want_until,
            "verification_status": r.verification_status, "rolled_back_at": None}

    proposed_side_effects: list[dict[str, Any]] = []
    for r in rows:
        a = actions.get(r.id, {"action": "keep"})
        if a["action"] == "mark_disputed_same_timestamp_conflict":
            a["expected"] = {"valid_from": r.observed_at, "valid_until": r.observed_at,
                             "verification_status": _DISPUTED, "rolled_back_at": None}
        elif a["action"] == "logical_rollback_exact_duplicate":
            a["expected"] = {"valid_from": r.valid_from, "valid_until": r.valid_until,
                             "verification_status": r.verification_status,
                             "rolled_back_at": _ROLLBACK_MARKER}
        elif "expected" not in a:
            a["expected"] = {"valid_from": r.valid_from, "valid_until": r.valid_until,
                             "verification_status": r.verification_status, "rolled_back_at": None}
        actions[r.id] = a

    # Proposed side effect: a create_price_anomaly per disputed conflict rep (no DB ids assigned).
    for r in disputed_reps:
        target = ident.row_hash(ident.row_values(r))
        proposed_side_effects.append({
            "type": "create_price_anomaly",
            "anomaly_type": _SAME_TIMESTAMP_CONFLICT, "severity": "high",
            "target_observation_ref": target,
            "deterministic_action_id": ident.row_hash(
                {"lane": lane_fp, "target": target, "type": _SAME_TIMESTAMP_CONFLICT}),
            "original_state": "absent",
            "restore_action": "delete_only_created_row",
        })

    sim_ok, sim_report = _simulate(rows, actions)
    if not sim_ok:
        entry["excluded"] = True
        entry["exclusion_reasons"].append("post_sim_invariant_fail")

    entry.update(
        excluded=entry["excluded"], apply_allowed=not entry["excluded"],
        exact_duplicate_groups=exact_groups,
        exact_duplicate_rows=sum(
            1 for a in actions.values() if a["action"] == "logical_rollback_exact_duplicate"),
        semantic_conflict_representatives=len(disputed_reps),
        semantic_conflict_groups=len(conflict_ts),
        facts_to_logically_rollback=sum(
            1 for a in actions.values() if a["action"] == "logical_rollback_exact_duplicate"),
        facts_to_mark_disputed=len(disputed_reps),
        intervals_to_reconstruct=reconstruct,
        occurrences_in_lane=sum(len(occ_by_obs.get(r.id, [])) for r in rows),
        rows=[_row_plan(r, fp[r.id], actions[r.id], occ_by_obs, supported_fk) for r in rows],
        proposed_side_effects=proposed_side_effects if not entry["excluded"] else [],
        projected_invariants=sim_report,
    )
    if entry["excluded"]:
        for r in entry["rows"]:
            r["action"] = "excluded_no_action"
        entry["proposed_actions"] = []
    entry["planned_changes"] = 0 if entry["excluded"] else sum(
        1 for a in actions.values() if a["action"] != "keep")
    return entry


def _simulate(rows, actions) -> tuple[bool, dict[str, Any]]:
    sims = []
    for r in rows:
        exp = actions[r.id]["expected"]
        if exp["rolled_back_at"] is not None:  # logically rolled back -> leaves the active set
            continue
        s = _sim_from(r)
        s.valid_from, s.valid_until = exp["valid_from"], exp["valid_until"]
        s.verification_status = exp["verification_status"]
        sims.append(s)
    report = lane_invariant_report(sims)
    ok = (report["lanes_multiple_open"] == 0 and report["lanes_overlapping_intervals"] == 0
          and report["lanes_repeated_timestamp"] == 0 and report["rows_non_positive_interval"] == 0
          and report["active_intervals_crossing_disputed"] == 0
          and report["disputed_rows_non_empty"] == 0)
    return ok, report


def _classify(action: str) -> str:
    return {
        "logical_rollback_exact_duplicate": "exact_duplicate_noncanonical",
        "mark_disputed_same_timestamp_conflict": "same_timestamp_semantic_conflict_representative",
        "reconstruct_interval": "sequential_unique",
        "keep": "sequential_unique_or_canonical",
        "excluded_no_action": "excluded",
    }[action]


def _jsonify(v):
    return json.loads(json.dumps(v, default=ident._json_default))


def _row_plan(row, fp, action, occ_by_obs, supported_fk) -> dict[str, Any]:
    original = ident.row_values(row)
    template = dict(original)
    for k, v in action["expected"].items():
        template[k] = v if v == _ROLLBACK_MARKER else _jsonify(v)
    if action["action"] == "logical_rollback_exact_duplicate":
        template["rolled_back_at"] = _ROLLBACK_MARKER
    occs = [{"values": _occ_values(o), "hash": ident.row_hash(_occ_values(o))}
            for o in occ_by_obs.get(row.id, [])]
    return {
        "id": row.id,
        "fact_fingerprint": fp,
        "classification": _classify(action["action"]),
        "action": action["action"],
        "original_values": original,
        "original_hash": ident.row_hash(original),
        # A TEMPLATE, not a real post-apply hash: it still contains <remediation_run_ts> (spec §5).
        "expected_state_template": action["expected"],
        "expected_template_hash": ident.row_hash(template),
        "occurrences": occs,
        "incoming_fk_state": supported_fk.get(row.id, []),
    }


# --------------------------------------------------------------------------- #
# Dry-run
# --------------------------------------------------------------------------- #
def dry_run(db: Session, provider_code: str | None = None) -> dict[str, Any]:
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
        anomalous = any(inv[k] for k in (
            "lanes_multiple_open", "lanes_overlapping_intervals", "lanes_repeated_timestamp",
            "rows_non_positive_interval", "active_intervals_crossing_disputed",
            "disputed_rows_non_empty"))
        counts["lanes_scanned"] += 1
        counts["ambiguous_provenance_scanned"] += _ambiguous_rows(rows, occ_by_obs)
        counts["fk_dependencies_scanned"] += sum(len(supported_fk.get(r.id, [])) for r in rows)
        if not anomalous:
            continue
        counts["lanes_anomalous"] += 1
        plan = _plan_lane(lane_fp, rows, occ_by_obs, supported_fk, unknown_fk)
        plan["anomalous"] = True
        lane_plans.append(plan)  # excluded lanes are INCLUDED in the manifest (spec §8)
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
    blockers: list[str] = []
    if any(v == "unknown" for v in provenance.values()):
        blockers.append("unknown_commit_provenance")
    if _writer_reuse_blocker():
        blockers.append("record_price_fact_may_reuse_rolled_back_exact_duplicate")
    apply_ready = not blockers

    manifest = _manifest(provider_code, retailer_id, baseline, lane_plans, discovered, provenance,
                         counts, apply_ready, blockers)
    report: dict[str, Any] = {k: int(v) for k, v in counts.items()}
    report["exclusion_reasons"] = dict(exclusion_reasons)
    report["projected_invariants_all_ok"] = all(
        _sim_report_ok(p["projected_invariants"]) for p in lane_plans if not p["excluded"])
    report["fk_discovered"] = [f"{f['schema']}.{f['table']}.{f['column']}" for f in discovered]
    report["fk_supported"] = sorted(
        {f["table"] for f in discovered if f["table"] in _HANDLED_FK_TABLES})
    report["fk_unknown"] = sorted(
        {f["table"] for f in discovered if f["table"] not in _HANDLED_FK_TABLES})
    report["apply_ready"] = apply_ready
    report["apply_blockers"] = blockers
    report.update(provenance)
    report["plan_hash"] = manifest["plan_hash"]
    return {"report": report, "manifest": manifest}


def _sim_report_ok(r) -> bool:
    return (r["lanes_multiple_open"] == 0 and r["lanes_overlapping_intervals"] == 0
            and r["lanes_repeated_timestamp"] == 0 and r["rows_non_positive_interval"] == 0
            and r["active_intervals_crossing_disputed"] == 0 and r["disputed_rows_non_empty"] == 0)


def _ambiguous_rows(rows, occ_by_obs) -> int:
    n = 0
    for r in rows:
        occs = occ_by_obs.get(r.id, [])
        if not occs or all(all(getattr(o, f) is None for f in _PROV_FIELDS) for o in occs):
            n += 1
    return n


def _manifest(provider_code, retailer_id, baseline, lane_plans, discovered, provenance, counts,
              apply_ready, blockers) -> dict[str, Any]:
    # plan_hash seals EVERY relevant input (spec §4), deterministically ordered, excluding only
    # generated_at / output paths / not-yet-bound execution timestamps.
    seal = {
        "schema_version": SCHEMA_VERSION,
        "tool_version": TOOL_VERSION,
        "commit_provenance": provenance,
        "provider_code": provider_code,
        "retailer_id": retailer_id,
        "baseline_counts": baseline,
        "fk_discovered": sorted(
            f"{f['schema']}.{f['table']}.{f['column']}:{f['supported']}" for f in discovered),
        "apply_ready": apply_ready,
        "apply_blockers": sorted(blockers),
        "lanes": sorted(
            (
                {
                    "lane_fingerprint": p["lane_fingerprint"],
                    "excluded": p["excluded"],
                    "exclusion_reasons": sorted(p["exclusion_reasons"]),
                    "rows": sorted(
                        (
                            {
                                "original_hash": r["original_hash"],
                                "action": r["action"],
                                "expected_template_hash": r["expected_template_hash"],
                                "occurrence_hashes": sorted(o["hash"] for o in r["occurrences"]),
                                "fk_hashes": sorted(
                                    fk["original_hash"] for fk in r["incoming_fk_state"]),
                            }
                            for r in p["rows"]
                        ),
                        key=lambda x: x["original_hash"],
                    ),
                    "proposed_side_effects": sorted(
                        s["deterministic_action_id"] for s in p["proposed_side_effects"]),
                }
                for p in lane_plans
            ),
            key=lambda x: x["lane_fingerprint"],
        ),
    }
    plan_hash = ident.row_hash(seal)
    return {
        "schema_version": SCHEMA_VERSION,
        "tool_version": TOOL_VERSION,
        "commit_provenance": provenance,
        "generated_at": datetime.now(UTC).isoformat(),
        "provider_code": provider_code,
        "retailer_id": retailer_id,
        "baseline_counts": baseline,
        "counts": {k: int(v) for k, v in counts.items()},
        "fk_discovered": discovered,
        "apply_ready": apply_ready,
        "apply_blockers": blockers,
        "apply_prerequisites": [
            "record_price_fact excludes rolled_back_at IS NULL (separate writer PR)",
            "all commit provenance known (planner/database/base_main)",
        ],
        "plan_hash": plan_hash,
        "lanes": lane_plans,
    }


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
        result = dry_run(db, a.provider)
        db.rollback()
    if a.manifest_path:
        with open(a.manifest_path, "w") as f:
            json.dump(result["manifest"], f, indent=2, default=str)
    json.dump(result["report"], sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
