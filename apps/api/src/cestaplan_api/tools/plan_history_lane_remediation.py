"""READ-ONLY, deterministic PLANNER for remediating legacy history-lane anomalies (design phase).

It NEVER writes: it only SELECTs, classifies each lane's rows, and proposes a reversible plan to
reconstruct a coherent interval chain WITHOUT deleting any fact or evidence. ``--apply`` is rejected
explicitly — no apply path exists yet. The plan only ever proposes changes to TEMPORAL-STATE fields
(valid_from/valid_until/verification_status/rolled_back_at/closed_by_run_id + an associated
PriceAnomaly); it never touches a fact-identity field or original provenance.

Proposed actions:
- exact duplicate facts (same full fingerprint): keep ONE canonical (stable policy), the rest get
  ``logical_rollback_exact_duplicate`` — rolled back (excluded from current-price selection), never
  deleted, occurrences and provenance preserved;
- same-timestamp semantic conflicts (same observed_at, different fingerprint): every fact ->
  ``disputed`` with an empty ``[T, T]`` interval + a ``same_timestamp_conflict`` PriceAnomaly;
- the surviving active facts are re-sequenced by anchors (each ends at the next anchor, a disputed
  barrier is never crossed, exactly one open row).

The dry-run emits a versioned, reversible manifest (full original row state + occurrences + incoming
FK + per-row classification + proposed actions + expected temporal state + original/expected
hashes +
a global ``plan_hash``) and a sanitized counts report. No secrets, URLs or raw payloads.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from cestaplan_api.db import SessionLocal
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

SCHEMA_VERSION = 1
TOOL_VERSION = "0.1.0-plan-only"

# Only these temporal-STATE fields may ever be proposed for change (spec §4). Everything else —
# every fact-identity field and all original provenance — is immutable.
MUTABLE_STATE_FIELDS = (
    "valid_from",
    "valid_until",
    "verification_status",
    "rolled_back_at",
    "rolled_back_by",
    "closed_by_run_id",
)

# A run-time value the apply tool would stamp; kept SYMBOLIC here so the plan stays deterministic.
_ROLLBACK_MARKER = "<remediation_run_ts>"
_DISPUTED = "disputed"
_SAME_TIMESTAMP_CONFLICT = "same_timestamp_conflict"

# Incoming FK tables we understand (and preserve). Any OTHER referencing table excludes the lane.
_KNOWN_FK = ("promotion_rule", "price_anomaly")

_STATUS_RANK = {"human_verified": 3, "machine_verified": 2, "unverified": 1, "disputed": 0}
_PROV_FIELDS = ("provider_code", "source_id", "crawl_run_id", "raw_capture_id")


def _deployed_sha() -> str:
    return os.environ.get("RAILWAY_GIT_COMMIT_SHA") or os.environ.get("GIT_SHA") or "unknown"


def _retailer_id(db: Session, provider_code: str) -> int | None:
    entry = get_entry(provider_code)
    slug = entry.retailer_slug if entry else provider_code
    return db.scalar(select(Retailer.id).where(Retailer.slug == slug))


# --------------------------------------------------------------------------- #
# Load (read-only)
# --------------------------------------------------------------------------- #
def _load(db: Session, provider_code: str | None):
    stmt = select(PriceObservation).where(
        PriceObservation.rolled_back_at.is_(None), PriceObservation.staging_only.is_(True)
    )
    retailer_id = None
    if provider_code is not None:
        retailer_id = _retailer_id(db, provider_code)
        if retailer_id is None:
            return {}, {}, {}, None
        stmt = stmt.where(PriceObservation.retailer_id == retailer_id)
    rows = list(db.execute(stmt).scalars())
    obs_ids = [r.id for r in rows]

    occ_by_obs: dict[int, list[PriceObservationOccurrence]] = defaultdict(list)
    if obs_ids:
        for occ in db.execute(
            select(PriceObservationOccurrence).where(
                PriceObservationOccurrence.price_observation_id.in_(obs_ids)
            )
        ).scalars():
            occ_by_obs[occ.price_observation_id].append(occ)

    fk_by_obs: dict[int, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    if obs_ids:
        for table, col in (
            ("promotion_rule", PromotionRule.price_observation_id),
            ("price_anomaly", PriceAnomaly.price_observation_id),
        ):
            for (oid,) in db.execute(
                select(col).where(col.in_(obs_ids))
            ).all():
                if oid is not None:
                    fk_by_obs[oid][table] += 1

    lanes: dict[str, list[PriceObservation]] = defaultdict(list)
    for r in rows:
        lanes[ident.price_history_lane_fingerprint(r)].append(r)
    return dict(lanes), dict(occ_by_obs), dict(fk_by_obs), retailer_id


# --------------------------------------------------------------------------- #
# Canonical policy (stable, deterministic)
# --------------------------------------------------------------------------- #
def _prov_completeness(occs: list[PriceObservationOccurrence]) -> int:
    """Best (max) count of demonstrable provenance fields across a row's occurrences."""
    best = 0
    for o in occs:
        best = max(best, sum(1 for f in _PROV_FIELDS if getattr(o, f) is not None))
    return best


def _verifiable_capture(row: PriceObservation, occs: list[PriceObservationOccurrence]) -> bool:
    if row.crawl_run_id is not None or row.raw_capture_id is not None:
        return True
    return any(o.crawl_run_id is not None or o.raw_capture_id is not None for o in occs)


def _canonical_key(row, occ_by_obs) -> tuple:
    occs = occ_by_obs.get(row.id, [])
    verif = _STATUS_RANK.get(row.verification_status or "", 0) * 1000 + int(
        (row.confidence_score or 0) * 100
    )
    # min() picks the canonical: more occurrences, more complete provenance, higher verification,
    # verifiable capture, then OLDEST imported_at, then smallest id (final deterministic tiebreak).
    return (
        -len(occs),
        -_prov_completeness(occs),
        -verif,
        -int(_verifiable_capture(row, occs)),
        row.imported_at,
        row.id,
    )


# --------------------------------------------------------------------------- #
# Classify + plan one lane
# --------------------------------------------------------------------------- #
def _sim_from(row: PriceObservation) -> SimpleNamespace:
    attrs = {f: getattr(row, f) for f in ident.LANE_FIELDS}
    attrs.update(
        id=row.id, observed_at=row.observed_at, valid_from=row.valid_from,
        valid_until=row.valid_until, verification_status=row.verification_status,
        rolled_back_at=row.rolled_back_at,
    )
    return SimpleNamespace(**attrs)


def _plan_lane(lane_fp, rows, occ_by_obs, fk_by_obs) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "lane_fingerprint": lane_fp, "excluded": False, "exclusion_reasons": []
    }

    # Gate: null timestamps make exact restore / sequencing impossible.
    if any(r.observed_at is None or r.valid_from is None for r in rows):
        entry["excluded"] = True
        entry["exclusion_reasons"].append("null_timestamp")
    # Gate: any incoming FK from an unknown table.
    for r in rows:
        for table in fk_by_obs.get(r.id, {}):
            if table not in _KNOWN_FK:
                entry["excluded"] = True
                entry["exclusion_reasons"].append(f"uncovered_fk:{table}")

    fp = {r.id: ident.price_fact_fingerprint(r) for r in rows}
    by_fp: dict[str, list] = defaultdict(list)
    for r in rows:
        by_fp[fp[r.id]].append(r)
    by_ts: dict[Any, set] = defaultdict(set)
    for r in rows:
        by_ts[r.observed_at].add(fp[r.id])

    # Same-timestamp SEMANTIC conflict = one observed_at carrying >1 distinct fingerprint.
    conflict_ts = {t for t, fps in by_ts.items() if len(fps) > 1}
    # Gate: a human-reviewed row inside a conflict needs manual review.
    if any(
        r.observed_at in conflict_ts and (r.verification_status == "human_verified") for r in rows
    ):
        entry["excluded"] = True
        entry["exclusion_reasons"].append("human_reviewed_conflict")

    actions: dict[int, dict[str, Any]] = {}
    canonical_ids: set[int] = set()
    exact_groups = 0
    for group in by_fp.values():
        if len(group) > 1:
            exact_groups += 1
        canonical = min(group, key=lambda r: _canonical_key(r, occ_by_obs))
        canonical_ids.add(canonical.id)
        for r in group:
            if r.id != canonical.id:
                actions[r.id] = {"action": "logical_rollback_exact_duplicate"}

    # Same-timestamp conflicts: EVERY fact at a conflict timestamp -> disputed empty [T,T].
    for r in rows:
        if r.observed_at in conflict_ts:
            actions[r.id] = {"action": "mark_disputed_same_timestamp_conflict"}

    # The active set after plan = canonical rows that are NOT rolled-back-dups and NOT disputed.
    active = [
        r for r in rows
        if r.id in canonical_ids
        and actions.get(r.id, {}).get("action") != "mark_disputed_same_timestamp_conflict"
    ]
    disputed = [r for r in rows if r.observed_at in conflict_ts]
    anchors = sorted({r.observed_at for r in active} | {r.observed_at for r in disputed})

    def next_anchor(t):
        after = [a for a in anchors if a > t]
        return after[0] if after else None

    reconstruct = 0
    for r in active:
        want_from = r.observed_at
        want_until = next_anchor(r.observed_at)
        prev = actions.get(r.id)
        if prev is None:
            if r.valid_from != want_from or r.valid_until != want_until:
                actions[r.id] = {"action": "reconstruct_interval"}
                reconstruct += 1
            else:
                actions[r.id] = {"action": "keep"}
        # expected temporal state for active rows:
        actions[r.id]["expected"] = {
            "valid_from": want_from, "valid_until": want_until,
            "verification_status": r.verification_status, "rolled_back_at": None,
        }
    for r in rows:
        a = actions.get(r.id, {"action": "keep"})
        if a["action"] == "mark_disputed_same_timestamp_conflict":
            a["expected"] = {
                "valid_from": r.observed_at, "valid_until": r.observed_at,
                "verification_status": _DISPUTED, "rolled_back_at": None,
            }
        elif a["action"] == "logical_rollback_exact_duplicate":
            a["expected"] = {
                "valid_from": r.valid_from, "valid_until": r.valid_until,
                "verification_status": r.verification_status, "rolled_back_at": _ROLLBACK_MARKER,
            }
        elif "expected" not in a:  # untouched keep
            a["expected"] = {
                "valid_from": r.valid_from, "valid_until": r.valid_until,
                "verification_status": r.verification_status, "rolled_back_at": None,
            }
        actions[r.id] = a

    # In-memory simulation -> verify invariants (spec §7). No DB, no flush.
    sim_ok, sim_report = _simulate(rows, actions)
    if not sim_ok:
        entry["excluded"] = True
        entry["exclusion_reasons"].append("post_sim_invariant_fail")

    entry.update(
        exact_duplicate_groups=exact_groups,
        exact_duplicate_rows=sum(
            1 for a in actions.values() if a["action"] == "logical_rollback_exact_duplicate"
        ),
        semantic_conflict_groups=len(conflict_ts),
        facts_to_logically_rollback=sum(
            1 for a in actions.values() if a["action"] == "logical_rollback_exact_duplicate"
        ),
        facts_to_mark_disputed=sum(
            1 for a in actions.values()
            if a["action"] == "mark_disputed_same_timestamp_conflict"
        ),
        intervals_to_reconstruct=reconstruct,
        occurrences_preserved=sum(len(occ_by_obs.get(r.id, [])) for r in rows),
        fk_dependencies=sum(sum(v.values()) for v in (fk_by_obs.get(r.id, {}) for r in rows)),
        rows=[_row_plan(r, fp[r.id], actions[r.id], occ_by_obs, fk_by_obs) for r in rows],
        projected_invariants=sim_report,
    )
    entry["planned_changes"] = sum(
        1 for a in actions.values() if a["action"] != "keep"
    )
    return entry


def _simulate(rows, actions) -> tuple[bool, dict[str, Any]]:
    """Apply the plan to in-memory copies and check the invariants. Never touches the DB."""
    sims = []
    for r in rows:
        exp = actions[r.id]["expected"]
        s = _sim_from(r)
        s.valid_from = exp["valid_from"]
        s.valid_until = exp["valid_until"]
        s.verification_status = exp["verification_status"]
        # A logically-rolled-back row leaves the non-rolled-back set entirely.
        rolled_back = exp["rolled_back_at"] is not None
        if not rolled_back:
            sims.append(s)
    report = lane_invariant_report(sims)
    ok = (
        report["lanes_multiple_open"] == 0
        and report["lanes_overlapping_intervals"] == 0
        and report["lanes_repeated_timestamp"] == 0
        and report["rows_non_positive_interval"] == 0
        and report["active_intervals_crossing_disputed"] == 0
        and report["disputed_rows_non_empty"] == 0
    )
    return ok, report


def _jsonify(v):
    """JSON-safe normalization (as row_values) so expected/original hashes are comparable."""
    return json.loads(json.dumps(v, default=ident._json_default))


def _row_plan(row, fp, action, occ_by_obs, fk_by_obs) -> dict[str, Any]:
    original = ident.row_values(row)
    expected_values = dict(original)
    for k, v in action["expected"].items():
        expected_values[k] = v if v == _ROLLBACK_MARKER else _jsonify(v)
    if action["action"] == "logical_rollback_exact_duplicate":
        expected_values["rolled_back_at"] = _ROLLBACK_MARKER
    return {
        "id": row.id,
        "fact_fingerprint": fp,
        "classification": _classify(action["action"]),
        "action": action["action"],
        "original_values": original,
        "original_hash": ident.row_hash(original),
        "expected_state": action["expected"],
        "expected_hash": ident.row_hash(expected_values),
        "occurrences": [
            {c.name: json.loads(json.dumps(getattr(o, c.name), default=str))
             for c in PriceObservationOccurrence.__table__.columns}
            for o in occ_by_obs.get(row.id, [])
        ],
        "incoming_fk": dict(fk_by_obs.get(row.id, {})),
    }


def _classify(action: str) -> str:
    return {
        "logical_rollback_exact_duplicate": "exact_duplicate_noncanonical",
        "mark_disputed_same_timestamp_conflict": "same_timestamp_semantic_conflict",
        "reconstruct_interval": "sequential_unique",
        "keep": "sequential_unique_or_canonical",
    }[action]


# --------------------------------------------------------------------------- #
# Dry-run entry
# --------------------------------------------------------------------------- #
def dry_run(db: Session, provider_code: str | None = None) -> dict[str, Any]:
    lanes, occ_by_obs, fk_by_obs, retailer_id = _load(db, provider_code)
    baseline = {
        "price_observation": int(
            db.scalar(select(func.count()).select_from(PriceObservation)) or 0),
        "price_observation_occurrence": int(
            db.scalar(select(func.count()).select_from(PriceObservationOccurrence)) or 0
        ),
    }
    lane_plans = []
    counts: dict[str, int] = dict.fromkeys(
        (
            "lanes_scanned", "lanes_anomalous", "lanes_plannable", "lanes_excluded",
            "exact_duplicate_groups", "exact_duplicate_rows", "semantic_conflict_groups",
            "facts_to_logically_rollback", "facts_to_mark_disputed", "intervals_to_reconstruct",
            "occurrences_preserved", "fk_dependencies", "manual_review_required",
            "ambiguous_provenance",
        ),
        0,
    )
    exclusion_reasons: dict[str, int] = defaultdict(int)
    projected_all_ok = True
    for lane_fp, rows in sorted(lanes.items()):
        report = lane_invariant_report(rows)
        anomalous = any(
            report[k] for k in (
                "lanes_multiple_open", "lanes_overlapping_intervals", "lanes_repeated_timestamp",
                "rows_non_positive_interval", "active_intervals_crossing_disputed",
                "disputed_rows_non_empty",
            )
        )
        counts["lanes_scanned"] += 1
        # Remediation targets ONLY the anomalous legacy lanes; a lane that already satisfies every
        # invariant is left untouched (planned_changes stays 0).
        if not anomalous:
            continue
        counts["lanes_anomalous"] += 1
        plan = _plan_lane(lane_fp, rows, occ_by_obs, fk_by_obs)
        if plan["excluded"]:
            counts["lanes_excluded"] += 1
            for reason in plan["exclusion_reasons"]:
                exclusion_reasons[reason] += 1
        else:
            counts["lanes_plannable"] += 1
            counts["exact_duplicate_groups"] += plan["exact_duplicate_groups"]
            counts["exact_duplicate_rows"] += plan["exact_duplicate_rows"]
            counts["semantic_conflict_groups"] += plan["semantic_conflict_groups"]
            counts["facts_to_logically_rollback"] += plan["facts_to_logically_rollback"]
            counts["facts_to_mark_disputed"] += plan["facts_to_mark_disputed"]
            counts["intervals_to_reconstruct"] += plan["intervals_to_reconstruct"]
            counts["occurrences_preserved"] += plan["occurrences_preserved"]
            counts["fk_dependencies"] += plan["fk_dependencies"]
            if not plan["projected_invariants"] or not _sim_report_ok(plan["projected_invariants"]):
                projected_all_ok = False
            lane_plans.append(plan)
    counts["manual_review_required"] = (
        exclusion_reasons.get("human_reviewed_conflict", 0)
        + sum(v for k, v in exclusion_reasons.items() if k.startswith("uncovered_fk"))
    )
    counts["ambiguous_provenance"] = _ambiguous_provenance(lane_plans)

    manifest = _manifest(provider_code, retailer_id, baseline, lane_plans)
    report: dict[str, Any] = {k: int(v) for k, v in counts.items()}
    report["exclusion_reasons"] = dict(exclusion_reasons)
    report["projected_invariants_all_ok"] = projected_all_ok
    report["plan_hash"] = manifest["plan_hash"]
    report["baseline"] = baseline
    return {"report": report, "manifest": manifest}


def _sim_report_ok(r: dict[str, Any]) -> bool:
    return (
        r["lanes_multiple_open"] == 0 and r["lanes_overlapping_intervals"] == 0
        and r["lanes_repeated_timestamp"] == 0 and r["rows_non_positive_interval"] == 0
        and r["active_intervals_crossing_disputed"] == 0 and r["disputed_rows_non_empty"] == 0
    )


def _ambiguous_provenance(lane_plans) -> int:
    n = 0
    for p in lane_plans:
        for row in p["rows"]:
            occs = row["occurrences"]
            if not occs or all(
                all(o.get(f) is None for f in _PROV_FIELDS) for o in occs
            ):
                n += 1
    return n


def _manifest(provider_code, retailer_id, baseline, lane_plans) -> dict[str, Any]:
    # plan_hash is deterministic: it hashes ONLY the ordered per-lane plan content (never the
    # wall-clock generated_at), so the same data always yields the same hash.
    plan_core = [
        {
            "lane_fingerprint": p["lane_fingerprint"],
            "rows": sorted(
                (
                    {"original_hash": r["original_hash"], "action": r["action"],
                     "expected_hash": r["expected_hash"]}
                    for r in p["rows"]
                ),
                key=lambda x: x["original_hash"],
            ),
        }
        for p in sorted(lane_plans, key=lambda p: p["lane_fingerprint"])
    ]
    plan_hash = ident.row_hash({"plan": plan_core})
    return {
        "schema_version": SCHEMA_VERSION,
        "tool_version": TOOL_VERSION,
        "source_commit_sha": _deployed_sha(),
        "generated_at": datetime.now(UTC).isoformat(),
        "provider_code": provider_code,
        "retailer_id": retailer_id,
        "baseline_counts": baseline,
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
            "plan (--dry-run). A separate, reviewed apply tool will consume the manifest."
        )
    with SessionLocal() as db:
        result = dry_run(db, a.provider)
        db.rollback()  # strictly read-only
    if a.manifest_path:
        with open(a.manifest_path, "w") as f:
            json.dump(result["manifest"], f, indent=2, default=str)
    json.dump(result["report"], sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
