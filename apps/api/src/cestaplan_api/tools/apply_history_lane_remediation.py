"""Reversible executor for the sealed history-lane remediation plan (apply spec).

DESIGN PHASE ONLY — running ``--apply`` against production is NOT authorized. This tool CONSUMES a
manifest produced by :mod:`cestaplan_api.tools.plan_history_lane_remediation` and executes exactly
the sealed, reviewed plan. It NEVER re-decides which row is canonical, never deletes a
``PriceObservation`` or ``PriceObservationOccurrence``, never relinks occurrences, and never touches
a fact-fingerprint field. Only the six temporal-state fields may change, plus a proposed
``PriceAnomaly`` and the durable audit rows.

Modes (spec §4): ``--verify-only`` (read-only), ``--simulate`` (in-memory, zero writes), ``--apply``
(implemented but blocked by default, needs explicit authorization + confirmations) and ``--restore``
(exact temporal restore of one run, deleting only the anomalies that run created).

Every gate is a typed exception (never ``assert`` — it holds under ``python -O``). Until immutable
build provenance is available, ``apply_ready`` is false with blocker
``immutable_build_provenance_missing`` (spec §12).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import event, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from cestaplan_api.config import Settings
from cestaplan_api.db import SessionLocal
from cestaplan_api.models import (
    CrawlJob,
    CrawlRun,
    PriceAnomaly,
    PriceObservation,
    ProductPrice,
    ProviderActivation,
    ProviderIngredientMapping,
)
from cestaplan_api.services import observation_identity as ident
from cestaplan_api.services import observation_persistence as writer
from cestaplan_api.tools import plan_history_lane_remediation as planner

if TYPE_CHECKING:
    # Annotations only — the audit models are imported lazily at runtime so the module loads even
    # where the migration is not yet applied (verify-only/simulate never touch the audit tables).
    from cestaplan_api.models import HistoryRemediationRun

APPLY_TOOL_VERSION = "0.1.0-apply"
REQUIRED_SCHEMA_VERSION = 4
REQUIRED_PLANNER_TOOL_VERSION = "0.4.0-plan-only"
REQUIRED_WRITER_CONTRACT = "record-price-fact-v2-active-only"
# The deployed writer must declare exactly these guarantees before any apply may execute (spec §1).
REQUIRED_WRITER_FLAGS = {
    "exact_fact_reuse_requires_rolled_back_at_null": True,
    "rolled_back_fact_never_receives_new_occurrence": True,
    "fresh_transient_candidate_required": True,
    "invalid_candidate_rejected_before_sql": True,
    "active_exact_ambiguity_policy": "fail_closed",
}

# The ONLY PriceObservation columns an apply may write (spec §8). Everything else — every fact-
# fingerprint field — is immutable, and DELETE is never allowed on facts or occurrences.
WHITELIST_FIELDS = planner.MUTABLE_STATE_FIELDS
_ROLLBACK_MARKER = planner._ROLLBACK_MARKER
_DISPUTED = "disputed"
_SUPPORTED_ACTIONS = frozenset({
    "keep", "excluded_no_action", "logical_rollback_exact_duplicate", "reconstruct_interval",
    "mark_disputed_same_timestamp_conflict",
})
_ACTION_WRITES = frozenset({
    "logical_rollback_exact_duplicate", "reconstruct_interval",
    "mark_disputed_same_timestamp_conflict",
})
# A fixed global advisory-lock key so at most one apply/restore runs at a time.
_GLOBAL_LOCK_KEY = ident.signed_bigint(
    hashlib.sha256(b"cestaplan:history-remediation:global").hexdigest())
# Tables an apply is allowed to write, and the audit/anomaly tables it may also touch (spec §8).
_AUDIT_TABLES = {"history_remediation_run", "history_remediation_change"}
_ANOMALY_TABLE = "price_anomaly"
_FORBIDDEN_TABLES = {"price_observation_occurrence"}
_MAX_PLAN_AGE_SECONDS = 24 * 3600  # a plan older than this is expired (spec §5)


# --------------------------------------------------------------------------- #
# Typed gate exceptions (never `assert`; hold under python -O) — spec §5.
# --------------------------------------------------------------------------- #
class ApplyError(RuntimeError):
    """Base for every executor gate failure. Carries a sanitized, stable ``code``."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        super().__init__(f"{code}: {detail}" if detail else code)


class ApplyManifestInvalid(ApplyError):
    """The manifest is unreadable, malformed, wrong-versioned, or carries sensitive data."""


class ApplyContractMismatch(ApplyError):
    """The deployed writer contract does not match record-price-fact-v2-active-only."""


class ApplyProvenanceMismatch(ApplyError):
    """Commit/build provenance is missing or does not line up across api/worker/main."""


class ApplyEnvironmentUnsafe(ApplyError):
    """A production-safety gate (production/flags/kill-switch/crawl/counts) is not satisfied."""


class ApplyPlanDrift(ApplyError):
    """The live database diverged from the sealed plan (row hash / occurrence / FK changed)."""


class ApplyUnsupportedAction(ApplyError):
    """The manifest carries an action or conflict this executor version does not support."""


class ApplyForbiddenWrite(ApplyError):
    """A write outside the strict whitelist / audit tables was attempted (interceptor tripped)."""


class ApplyRequiresPostgres(ApplyError):
    """The bind is not PostgreSQL."""


class ApplyAlreadyApplied(ApplyError):
    """This plan_hash already completed an apply (idempotency §9)."""


class ApplyAlreadyRestored(ApplyError):
    """This run was already restored (idempotency §9)."""


class ApplyNotAuthorized(ApplyError):
    """--apply/--restore invoked without the explicit authorization + confirmations."""


class ApplyLockUnavailable(ApplyError):
    """A remediation advisory lock could not be acquired within the timeout."""


class ApplyBackupMissing(ApplyError):
    """A verified backup reference is required before an apply may execute."""


class ApplyRestoreDrift(ApplyError):
    """A row changed after the apply, so an exact restore is impossible — manual review required."""


# --------------------------------------------------------------------------- #
# Immutable build provenance (spec §3/§12)
# --------------------------------------------------------------------------- #
def _immutable_build_hash() -> str | None:
    """Return an IMMUTABLE build-provenance hash, or None if none is available.

    Preference: a build-time env (``SOURCE_TREE_HASH`` / ``BUILD_ARTIFACT_HASH``) or a file written
    during the image build (``BUILD_PROVENANCE_PATH``). ``APP_COMMIT_SHA`` alone is a mutable
    operational declaration and is intentionally NOT accepted as the immutable evidence.
    """
    for var in ("SOURCE_TREE_HASH", "BUILD_ARTIFACT_HASH"):
        val = os.environ.get(var)
        if val:
            return val.strip()
    path = os.environ.get("BUILD_PROVENANCE_PATH")
    if path and Path(path).is_file():
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    return None


@dataclass(slots=True)
class ApplyContext:
    """What the executor needs beyond the DB session and manifest (injected, never guessed)."""

    app_commit_sha: str | None = None
    immutable_build_hash: str | None = None
    deployed_api_sha: str | None = None
    deployed_worker_sha: str | None = None
    expected_main_sha: str | None = None
    expected_alembic: str | None = None
    # ProductPrice / active mappings expected counts — 0 unless a future manifest authorizes a
    # change (spec §5). The gate compares the live count to these, never a hardcoded 0.
    expected_product_price: int = 0
    expected_active_mappings: int = 0
    backup_sha256: str | None = None
    backup_verified: bool = False
    operator_reference: str | None = None
    now: datetime | None = None

    @classmethod
    def from_environment(cls, **overrides: Any) -> ApplyContext:
        base = cls(
            app_commit_sha=os.environ.get("APP_COMMIT_SHA"),
            immutable_build_hash=_immutable_build_hash(),
            deployed_api_sha=os.environ.get("DEPLOYED_API_SHA") or os.environ.get("APP_COMMIT_SHA"),
            deployed_worker_sha=os.environ.get("DEPLOYED_WORKER_SHA"),
            expected_main_sha=os.environ.get("EXPECTED_MAIN_SHA"),
            expected_alembic=os.environ.get("EXPECTED_ALEMBIC_REVISION"),
        )
        for k, v in overrides.items():
            setattr(base, k, v)
        return base


# --------------------------------------------------------------------------- #
# Manifest loading + contract validation + plan_hash recompute (spec §1/§5)
# --------------------------------------------------------------------------- #
def load_manifest(path: str) -> dict[str, Any]:
    try:
        raw = Path(path).read_text()
    except OSError as exc:
        raise ApplyManifestInvalid("manifest_unreadable", str(exc)) from exc
    try:
        manifest = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ApplyManifestInvalid("manifest_not_json", str(exc)) from exc
    if not isinstance(manifest, dict):
        raise ApplyManifestInvalid("manifest_not_object")
    return manifest


def _require_manifest_shape(m: dict[str, Any]) -> None:
    if m.get("schema_version") != REQUIRED_SCHEMA_VERSION:
        raise ApplyManifestInvalid("manifest_schema_version", str(m.get("schema_version")))
    if m.get("tool_version") != REQUIRED_PLANNER_TOOL_VERSION:
        raise ApplyManifestInvalid("manifest_tool_version", str(m.get("tool_version")))
    for key in ("plan_hash", "lanes", "commit_provenance", "planner_source_hash",
                "apply_blockers", "apply_prerequisites", "baseline_counts", "fk_discovered"):
        if key not in m:
            raise ApplyManifestInvalid("manifest_missing_key", key)
    if planner.scan_sensitive(m):
        raise ApplyManifestInvalid("manifest_sensitive_data")


def _recompute_plan_hash(m: dict[str, Any]) -> str:
    prov = {k: v for k, v in m["commit_provenance"].items() if k != "complete"}
    return planner._seal(
        m.get("provider_code"), m.get("retailer_id"), m["baseline_counts"], m["lanes"],
        m["fk_discovered"], prov, m["commit_provenance"].get("complete", False),
        m["apply_blockers"], m["apply_prerequisites"], m["planner_source_hash"])


# --------------------------------------------------------------------------- #
# Deployed-writer contract gate (spec §1)
# --------------------------------------------------------------------------- #
def _writer_contract_gate() -> tuple[bool, str]:
    c = writer.writer_contract()
    if c.get("version") != REQUIRED_WRITER_CONTRACT:
        return False, "writer_contract_version"
    for k, v in REQUIRED_WRITER_FLAGS.items():
        if c.get(k) != v:
            return False, f"writer_contract_flag:{k}"
    return True, "writer_contract_v2"


# --------------------------------------------------------------------------- #
# Environment / DB safety gates (spec §5)
# --------------------------------------------------------------------------- #
_CHAINS = ("parsebot-alcampo", "parsebot-dia", "parsebot-carrefour", "parsebot-lidl",
           "parsebot-aldi", "parsebot-deza", "apify-mercadona")


def _require_postgres(db: Session) -> None:
    bind = db.bind
    if bind is None or bind.dialect.name != "postgresql":
        raise ApplyRequiresPostgres("requires_postgres")


def _alembic_revision(db: Session) -> str | None:
    return db.execute(text("SELECT version_num FROM alembic_version")).scalar()


def _kill_switch_active(settings: Settings, acts: dict[str, Any]) -> bool:
    env = os.environ.get("PRICE_PROVIDER_KILL_SWITCH", "").strip().lower()
    if env in ("1", "true", "on", "yes"):
        return True
    # Equivalent gate: every provider path is disabled (production off AND every chain flag off).
    return not _production_enabled(settings, acts) and _flags_all_false(settings)


def _flags_all_false(s: Settings) -> bool:
    return not any([
        s.parse_bot_alcampo_enabled, s.parse_bot_dia_enabled, s.parse_bot_carrefour_enabled,
        s.parse_bot_lidl_enabled, s.parse_bot_aldi_enabled, s.parse_bot_deza_enabled,
        s.apify_enabled, s.apify_mercadona_enabled])


def _production_enabled(s: Settings, acts: dict[str, Any]) -> bool:
    return any((acts[ch].production_enabled or acts[ch].production_approved)
               for ch in _CHAINS if ch in acts)


def _environment_gates(
        db: Session, settings: Settings, ctx: ApplyContext) -> list[tuple[str, bool]]:
    def count(model, *w) -> int:
        q = select(func.count()).select_from(model)
        for x in w:
            q = q.where(x)
        return int(db.scalar(q) or 0)

    acts = {a.provider_code: a for a in db.execute(select(ProviderActivation)).scalars()}
    runs = {st: int(n) for st, n in db.execute(
        select(CrawlRun.status, func.count(CrawlRun.id)).group_by(CrawlRun.status)).all()}
    jobs = {st: int(n) for st, n in db.execute(
        select(CrawlJob.status, func.count(CrawlJob.id)).group_by(CrawlJob.status)).all()}
    return [
        ("production_disabled", not _production_enabled(settings, acts)),
        ("per_chain_flags_false", _flags_all_false(settings)),
        ("price_provider_kill_switch", _kill_switch_active(settings, acts)),
        ("crawl_run_not_running", runs.get("running", 0) == 0),
        ("crawl_job_not_active",
         (jobs.get("queued", 0) + jobs.get("locked", 0) + jobs.get("running", 0)) == 0),
        ("product_price_matches_expected", count(ProductPrice) == ctx.expected_product_price),
        ("mappings_match_expected",
         count(ProviderIngredientMapping,
               ProviderIngredientMapping.active.is_(True)) == ctx.expected_active_mappings),
        ("alembic_revision",
         ctx.expected_alembic is not None and _alembic_revision(db) == ctx.expected_alembic),
    ]


# --------------------------------------------------------------------------- #
# Provenance gates (spec §3)
# --------------------------------------------------------------------------- #
def _provenance_gates(m: dict[str, Any], ctx: ApplyContext) -> list[tuple[str, bool]]:
    api = ctx.deployed_api_sha
    worker = ctx.deployed_worker_sha
    app = ctx.app_commit_sha
    expected = ctx.expected_main_sha or m["commit_provenance"].get("base_main_sha")
    aligned = bool(app) and api == app and worker == app
    return [
        ("app_commit_sha_present", bool(app)),
        ("immutable_build_provenance", bool(ctx.immutable_build_hash)),
        ("api_worker_aligned", aligned),
        ("main_commit_sha_matches",
         bool(expected) and expected not in ("unknown", None) and app == expected),
    ]


# --------------------------------------------------------------------------- #
# Live-DB drift revalidation vs the sealed plan (spec §5)
# --------------------------------------------------------------------------- #
def _planned_rows(m: dict[str, Any]) -> list[dict[str, Any]]:
    """Every non-excluded lane's rows (the rows an apply may touch)."""
    out = []
    for lane in m["lanes"]:
        if lane.get("excluded"):
            continue
        for r in lane["rows"]:
            out.append(r)
    return out


def _drift_gates(db: Session, m: dict[str, Any]) -> list[tuple[str, bool]]:
    ok_rows = ok_occ = ok_fk = True
    obs_ids = [r["id"] for r in _planned_rows(m)]
    live = {o.id: o for o in db.execute(
        select(PriceObservation).where(PriceObservation.id.in_(obs_ids))).scalars()} \
        if obs_ids else {}
    for r in _planned_rows(m):
        row = live.get(r["id"])
        if row is None:
            ok_rows = False
            continue
        if planner._split_row(row)[2]["full_row_hash"] != r["integrity"]["full_row_hash"]:
            ok_rows = False  # row changed since the plan (or the sealed hash was altered)
        live_occ_hashes = {
            planner._occ_manifest(o)["occurrence_hash"]
            for o in db.execute(select(planner.PriceObservationOccurrence).where(
                planner.PriceObservationOccurrence.price_observation_id == r["id"])).scalars()}
        if live_occ_hashes != {o["occurrence_hash"] for o in r["occurrences"]}:
            ok_occ = False  # an occurrence was added/removed/changed after the plan
    # Supported-FK rows unchanged and zero unknown FKs, re-derived live.
    discovered = planner.discover_incoming_fks(db)
    if obs_ids and planner._unknown_fk_refs(db, discovered, obs_ids):
        ok_fk = False
    return [("row_hashes_match", ok_rows), ("occurrences_unchanged", ok_occ),
            ("no_unknown_fk", ok_fk)]


# --------------------------------------------------------------------------- #
# SQL write guard — defense-in-depth interceptor (spec §8)
# --------------------------------------------------------------------------- #
# price_observation UPDATEs may set the six temporal fields plus the ORM-managed ``updated_at``
# audit stamp (never a fact-identity column).
_ALLOWED_PO_UPDATE_COLS = set(WHITELIST_FIELDS) | {"updated_at"}
_DML_RE = re.compile(
    r"^\s*(insert\s+into|update|delete\s+from)\s+(?:only\s+)?\"?(?:public\.)?\"?"
    r"([a-z_][a-z0-9_]*)\"?", re.IGNORECASE)
_SET_COLS_RE = re.compile(r"\bset\b(.*?)(?:\bwhere\b|\breturning\b|$)", re.IGNORECASE | re.DOTALL)
_COL_RE = re.compile(r'["\s,]*"?([a-z_][a-z0-9_]*)"?\s*=')


def _forbid(statement: str) -> None:
    stmt = statement.strip()
    mm = _DML_RE.match(stmt)
    if not mm:  # not INSERT/UPDATE/DELETE (SELECT, SET, SHOW, SAVEPOINT, …) -> allowed
        return
    op = mm.group(1).lower().split()[0]
    table = mm.group(2).lower()
    if table in _FORBIDDEN_TABLES:
        raise ApplyForbiddenWrite("forbidden_write_occurrence", table)
    if op == "delete":
        if table == _ANOMALY_TABLE and _write_guard_state.get("allow_anomaly_delete"):
            return
        raise ApplyForbiddenWrite("forbidden_delete", table)
    if op == "insert":
        if table in _AUDIT_TABLES or table == _ANOMALY_TABLE:
            return
        raise ApplyForbiddenWrite("forbidden_insert", table)
    # UPDATE
    if table in _AUDIT_TABLES:
        return
    if table == "price_observation":
        body = _SET_COLS_RE.search(stmt)
        cols = {c.lower() for c in _COL_RE.findall(body.group(1))} if body else set()
        if cols and cols <= _ALLOWED_PO_UPDATE_COLS:
            return
        raise ApplyForbiddenWrite("forbidden_update_columns",
                                  ",".join(sorted(cols - _ALLOWED_PO_UPDATE_COLS)) or "unparsed")
    raise ApplyForbiddenWrite("forbidden_update", table)


_write_guard_state: dict[str, bool] = {}


class _WriteGuard:
    """Attach an interceptor that FAILS on any DML outside the whitelist for the duration."""

    def __init__(self, db: Session, *, allow_anomaly_delete: bool = False) -> None:
        self._conn = db.connection()
        self._allow = allow_anomaly_delete

    def __enter__(self) -> _WriteGuard:
        _write_guard_state["allow_anomaly_delete"] = self._allow

        def _before(conn, cursor, statement, params, context, executemany):
            _forbid(statement)

        self._listener = _before
        event.listen(self._conn.engine, "before_cursor_execute", self._listener)
        return self

    def __exit__(self, *exc: Any) -> None:
        event.remove(self._conn.engine, "before_cursor_execute", self._listener)
        _write_guard_state.pop("allow_anomaly_delete", None)


# --------------------------------------------------------------------------- #
# Bind the sealed template to concrete values (spec §1/§10)
# --------------------------------------------------------------------------- #
def _parse_dt(v: Any) -> Any:
    if isinstance(v, str):
        return datetime.fromisoformat(v.replace("Z", "+00:00"))
    return v


def _bound_temporal(live_row: PriceObservation, row_m: dict[str, Any], *,
                    run_ts: datetime) -> dict[str, Any]:
    """The exact target temporal state, binding ``<remediation_run_ts>`` to this run (spec §1).

    Only the fields the sealed template names change; ``rolled_back_by`` (a FK to ``user``) is NOT
    part of the plan, so it is left untouched — run attribution lives in the audit tables.
    """
    bound = {f: getattr(live_row, f) for f in WHITELIST_FIELDS}
    tmpl = row_m["expected_state_template"]
    for k in WHITELIST_FIELDS:
        if k not in tmpl:
            continue
        v = tmpl[k]
        if v == _ROLLBACK_MARKER:
            bound[k] = run_ts
        elif k in ("valid_from", "valid_until", "rolled_back_at"):
            bound[k] = _parse_dt(v)
        else:
            bound[k] = v
    return bound


def _temporal_of(row: PriceObservation) -> dict[str, Any]:
    return {f: getattr(row, f) for f in WHITELIST_FIELDS}


def _norm_state(state: dict[str, Any]) -> dict[str, Any]:
    """Normalize a temporal state for STABLE hashing/storage: every datetime is compared as one UTC
    instant, never in the connection's local offset (which the DB may echo back after a flush)."""
    out: dict[str, Any] = {}
    for k, v in state.items():
        if isinstance(v, datetime):
            out[k] = v.astimezone(UTC).isoformat()
        elif isinstance(v, str) and k in ("valid_from", "valid_until", "rolled_back_at"):
            out[k] = _parse_dt(v).astimezone(UTC).isoformat()
        else:
            out[k] = v
    return out


def _thash(state: dict[str, Any]) -> str:
    return planner._value_hash(_norm_state(state))


# --------------------------------------------------------------------------- #
# In-memory simulation (spec §4B) — invariants on copies, ZERO writes
# --------------------------------------------------------------------------- #
def _simulate_plan(m: dict[str, Any]) -> dict[str, Any]:
    """Validate the plan's own sealed projection WITHOUT building ORM objects (spec §4B/§12): the
    planner already computed each lane's post-plan invariants; a tampered projection is caught by
    plan_hash gate, and here we re-assert every non-excluded lane projects a coherent history."""
    lanes_ok = True
    total_changes = 0
    for lane in m["lanes"]:
        if lane.get("excluded"):
            continue
        total_changes += sum(1 for r in lane["rows"] if r["action"] in _ACTION_WRITES)
        proj = lane.get("projected_invariants") or {}
        if not proj or not planner._sim_report_ok(proj):
            lanes_ok = False
    return {"simulated_invariants_ok": lanes_ok, "planned_changes": total_changes}


# --------------------------------------------------------------------------- #
# Gate driver: collect (verify) or fail-closed (apply)
# --------------------------------------------------------------------------- #
def _run_all_gates(db: Session, m: dict[str, Any], ctx: ApplyContext, settings: Settings,
                   *, for_apply: bool) -> tuple[list[str], list[str]]:
    passed: list[str] = []
    blocking: list[str] = []

    def record(code: str, ok: bool) -> None:
        (passed if ok else blocking).append(code)

    record("plan_hash_intact", _recompute_plan_hash(m) == m["plan_hash"])
    record("plan_not_expired", _plan_age_ok(m, ctx))
    record("supported_actions_only", _actions_supported(m))
    wgate_ok, wgate_code = _writer_contract_gate()
    record(wgate_code if wgate_ok else "writer_contract_v2", wgate_ok)
    for code, ok in _provenance_gates(m, ctx):
        record(code, ok)
    for code, ok in _environment_gates(db, settings, ctx):
        record(code, ok)
    for code, ok in _drift_gates(db, m):
        record(code, ok)
    if for_apply:
        record("backup_verified", ctx.backup_verified and bool(ctx.backup_sha256))
    return passed, blocking


def _plan_age_ok(m: dict[str, Any], ctx: ApplyContext) -> bool:
    gen = m.get("generated_at")
    if not gen:
        return False
    now = ctx.now or datetime.now(UTC)
    try:
        age = (now - _parse_dt(gen)).total_seconds()
    except (ValueError, TypeError):
        return False
    return 0 <= age <= _MAX_PLAN_AGE_SECONDS


def _actions_supported(m: dict[str, Any]) -> bool:
    for lane in m["lanes"]:
        for r in lane["rows"]:
            if r["action"] not in _SUPPORTED_ACTIONS:
                return False
        for se in lane.get("proposed_side_effects", []):
            if se.get("type") != "create_price_anomaly":
                return False
    return True


# --------------------------------------------------------------------------- #
# Public modes
# --------------------------------------------------------------------------- #
def verify_only(db: Session, m: dict[str, Any], ctx: ApplyContext,
                settings: Settings | None = None) -> dict[str, Any]:
    """Read-only validation (spec §4A): validate manifest, hashes, DB, contracts. ZERO writes."""
    _require_postgres(db)
    _require_manifest_shape(m)
    settings = settings or Settings()
    passed, blocking = _run_all_gates(db, m, ctx, settings, for_apply=True)
    sim = {"planned_changes": sum(
        1 for r in _planned_rows(m) if r["action"] in _ACTION_WRITES)}
    apply_ready = not blocking
    report = {
        "apply_tool_version": APPLY_TOOL_VERSION,
        "plan_found": True,
        "plan_hash": m["plan_hash"],
        "manifest_schema_version": m["schema_version"],
        "planner_tool_version": m["tool_version"],
        "writer_contract": writer.writer_contract().get("version"),
        "declared_commit_sha": ctx.app_commit_sha,
        "immutable_build_hash": ctx.immutable_build_hash,
        "lanes": len(m["lanes"]),
        "lanes_excluded": sum(1 for x in m["lanes"] if x.get("excluded")),
        "planned_changes": sim["planned_changes"],
        "gates_passed": sorted(passed),
        "gates_blocking": sorted(blocking),
        "apply_ready": apply_ready,
    }
    if not ctx.immutable_build_hash:
        report["apply_ready"] = False
        report.setdefault("apply_blockers", []).append("immutable_build_provenance_missing")
    return report


def simulate(db: Session, m: dict[str, Any], ctx: ApplyContext,
             settings: Settings | None = None) -> dict[str, Any]:
    """In-memory transformation + invariant check (spec §4B). ZERO writes."""
    _require_postgres(db)
    _require_manifest_shape(m)
    settings = settings or Settings()
    passed, blocking = _run_all_gates(db, m, ctx, settings, for_apply=False)
    sim = _simulate_plan(m)
    return {
        "apply_tool_version": APPLY_TOOL_VERSION,
        "plan_hash": m["plan_hash"],
        "gates_passed": sorted(passed),
        "gates_blocking": sorted(blocking),
        **sim,
    }


def _acquire(db: Session, key: int, *, timeout_ms: int) -> None:
    db.execute(text(f"SET LOCAL lock_timeout = '{int(timeout_ms)}ms'"))
    try:
        db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": key})
    except Exception as exc:
        raise ApplyLockUnavailable("lock_timeout", str(exc)[:80]) from exc


def _count_snapshot(db: Session) -> dict[str, int]:
    def c(model, *w) -> int:
        q = select(func.count()).select_from(model)
        for x in w:
            q = q.where(x)
        return int(db.scalar(q) or 0)
    return {
        "price_observation": c(PriceObservation),
        "price_observation_occurrence": c(planner.PriceObservationOccurrence),
        "rolled_back": c(PriceObservation, PriceObservation.rolled_back_at.is_not(None)),
        "price_anomaly": c(PriceAnomaly),
    }


def apply(db: Session, m: dict[str, Any], ctx: ApplyContext, *, authorized: bool = False,
          confirmations: tuple[str, ...] = (), settings: Settings | None = None,
          lock_timeout_ms: int = 5000) -> dict[str, Any]:
    """Execute the sealed plan atomically (spec §4C/§7). BLOCKED by default: requires explicit
    authorization + the exact confirmation tokens. Does not auto-commit; the caller controls the
    single transaction so any failure rolls back the whole run."""
    from cestaplan_api.models import HistoryRemediationRun
    _require_authorization(authorized, confirmations)
    _require_postgres(db)
    _require_manifest_shape(m)
    settings = settings or Settings()

    # 1) global remediation lock; 2) deterministic lane locks; 3) full revalidation under the locks.
    _acquire(db, _GLOBAL_LOCK_KEY, timeout_ms=lock_timeout_ms)
    if _completed_run(db, m["plan_hash"]) is not None:
        return {"status": "already_applied", "plan_hash": m["plan_hash"]}
    for lane in sorted(m["lanes"], key=lambda x: x["lane_fingerprint"]):
        if not lane.get("excluded"):
            _acquire(db, ident.signed_bigint(hashlib.sha256(
                lane["lane_fingerprint"].encode()).hexdigest()), timeout_ms=lock_timeout_ms)
    _passed, blocking = _run_all_gates(db, m, ctx, settings, for_apply=True)
    if blocking:
        raise ApplyEnvironmentUnsafe("gates_blocking", ",".join(sorted(blocking)))

    run_ts = ctx.now or datetime.now(UTC)
    before = _count_snapshot(db)
    run = HistoryRemediationRun(
        plan_hash=m["plan_hash"], manifest_schema_version=m["schema_version"],
        planner_tool_version=m["tool_version"], planner_source_hash=m["planner_source_hash"],
        writer_contract_version=REQUIRED_WRITER_CONTRACT,
        main_commit_sha=ctx.expected_main_sha or m["commit_provenance"].get("base_main_sha") or "",
        deployed_api_sha=ctx.deployed_api_sha, deployed_worker_sha=ctx.deployed_worker_sha,
        alembic_revision=_alembic_revision(db) or "", execution_mode="apply", status="applied",
        started_at=run_ts, operator_reference=ctx.operator_reference,
        backup_sha256=ctx.backup_sha256, before_counts=before)
    db.add(run)
    try:
        db.flush()  # unique(plan_hash where status=applied) -> concurrent duplicate fails here
    except IntegrityError as exc:
        raise ApplyAlreadyApplied("plan_hash_already_applied", str(exc)[:80]) from exc
    run_ref = str(run.public_id)

    changes: list[dict[str, Any]] = []
    with _WriteGuard(db):
        live = {o.id: o for o in db.execute(select(PriceObservation).where(
            PriceObservation.id.in_([r["id"] for r in _planned_rows(m)])).with_for_update()
        ).scalars()}
        for lane in m["lanes"]:
            if lane.get("excluded"):
                continue
            anomaly_by_target = _apply_side_effects(db, lane)
            for r in lane["rows"]:
                changes.append(_apply_row(db, run, r, live[r["id"]], run_ts, anomaly_by_target))
        db.flush()
        # 5) post-flush verification: every WRITTEN row now matches its bound expectation exactly.
        for ch in changes:
            if ch["action_type"] not in _ACTION_WRITES:
                continue
            actual = _thash(_temporal_of(live[ch["price_observation_id"]]))
            if actual != ch["expected_bound_hash"]:
                raise ApplyPlanDrift("post_flush_mismatch", str(ch["price_observation_id"]))
    after = _count_snapshot(db)
    run.after_counts = after
    run.completed_at = ctx.now or datetime.now(UTC)
    run.execution_hash = _value_execution_hash(m["plan_hash"], changes)
    _assert_counts_preserved(before, after)
    db.flush()
    return {"status": "applied", "plan_hash": m["plan_hash"], "run_public_id": run_ref,
            "changes": len(changes), "before_counts": before, "after_counts": after}


def _apply_side_effects(db: Session, lane: dict[str, Any]) -> dict[str, PriceAnomaly]:
    """Create ONLY the manifest-proposed anomalies, keyed by their target row full_row_hash."""
    out: dict[str, PriceAnomaly] = {}
    for se in lane.get("proposed_side_effects", []):
        if se.get("type") != "create_price_anomaly":
            raise ApplyUnsupportedAction("unsupported_side_effect", str(se.get("type")))
        target = se["target_observation_ref"]
        an = PriceAnomaly(price_observation_id=_obs_id_for_hash(lane, target),
                          anomaly_type=se["anomaly_type"], severity=se["severity"], status="open")
        db.add(an)
        db.flush()
        out[target] = an
    return out


def _obs_id_for_hash(lane: dict[str, Any], full_row_hash: str) -> int | None:
    for r in lane["rows"]:
        if r["integrity"]["full_row_hash"] == full_row_hash:
            return r["id"]
    return None


def _apply_row(db: Session, run: HistoryRemediationRun, r: dict[str, Any],
               live: PriceObservation, run_ts: datetime,
               anomaly_by_target: dict[str, PriceAnomaly]) -> dict[str, Any]:
    from cestaplan_api.models import HistoryRemediationChange
    action = r["action"]
    if action not in _SUPPORTED_ACTIONS:
        raise ApplyUnsupportedAction("unsupported_action", action)
    original = _temporal_of(live)
    if action in _ACTION_WRITES:
        bound = _bound_temporal(live, r, run_ts=run_ts)
        for k in WHITELIST_FIELDS:
            if bound[k] != original[k]:
                setattr(live, k, bound[k])
    else:
        bound = original
    anomaly = anomaly_by_target.get(r["integrity"]["full_row_hash"])
    ch = HistoryRemediationChange(
        remediation_run_id=run.id, deterministic_action_id=r.get("fact_fingerprint", ""),
        price_observation_id=r["id"], action_type=action,
        original_temporal_state=_json(original), expected_bound_state=_json(bound),
        original_hash=r["integrity"]["full_row_hash"], expected_bound_hash=_thash(bound),
        created_anomaly_id=anomaly.id if anomaly is not None else None,
        status="applied" if action in _ACTION_WRITES else "planned")
    db.add(ch)
    return {"price_observation_id": r["id"], "action_type": action,
            "expected_bound_hash": _thash(bound)}


def restore(db: Session, run_public_id: str, ctx: ApplyContext, *, authorized: bool = False,
            confirmations: tuple[str, ...] = (), lock_timeout_ms: int = 5000) -> dict[str, Any]:
    """Exactly restore one apply run's original temporal state and delete only the anomalies it
    created (spec §4D/§10). Facts and occurrences are never touched. Fails closed on any drift."""
    from cestaplan_api.models import HistoryRemediationChange, HistoryRemediationRun
    _require_authorization(authorized, confirmations, restore=True)
    _require_postgres(db)
    _acquire(db, _GLOBAL_LOCK_KEY, timeout_ms=lock_timeout_ms)
    run = db.execute(select(HistoryRemediationRun).where(
        HistoryRemediationRun.public_id == run_public_id)).scalar_one_or_none()
    if run is None:
        raise ApplyManifestInvalid("run_not_found", run_public_id)
    if run.restore_status == "restored":
        return {"status": "already_restored", "run_public_id": run_public_id}
    if run.status != "applied":
        raise ApplyRestoreDrift("run_not_applied", run.status)
    changes = list(db.execute(select(HistoryRemediationChange).where(
        HistoryRemediationChange.remediation_run_id == run.id)).scalars())
    with _WriteGuard(db, allow_anomaly_delete=True):
        rows = {o.id: o for o in db.execute(select(PriceObservation).where(
            PriceObservation.id.in_([c.price_observation_id for c in changes])
        ).with_for_update()).scalars()}
        for ch in changes:
            row = rows[ch.price_observation_id]
            if ch.action_type in _ACTION_WRITES and \
                    _thash(_temporal_of(row)) != ch.expected_bound_hash:
                run.restore_status = "manual_review_required"
                raise ApplyRestoreDrift("row_changed_after_apply", str(ch.price_observation_id))
            for k in WHITELIST_FIELDS:
                setattr(row, k, _parse_dt(ch.original_temporal_state.get(k))
                        if k in ("valid_from", "valid_until", "rolled_back_at")
                        else ch.original_temporal_state.get(k))
            ch.restore_state = _json(_temporal_of(row))
            ch.status = "restored"
        # Delete ONLY the anomalies this run created; null the audit ref first so the FK holds, then
        # delete. Facts and occurrences are never touched.
        for ch in changes:
            if ch.created_anomaly_id is None:
                continue
            an = db.get(PriceAnomaly, ch.created_anomaly_id)
            ch.created_anomaly_id = None
            db.flush()
            if an is not None:
                db.delete(an)
        db.flush()
    run.restore_status = "restored"
    run.status = "rolled_back"
    db.flush()
    return {"status": "restored", "run_public_id": run_public_id, "restored_rows": len(changes)}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _require_authorization(authorized: bool, confirmations: tuple[str, ...],
                           *, restore: bool = False) -> None:
    needed = ("I_UNDERSTAND_THIS_WRITES", "PLAN_REVIEWED", "BACKUP_VERIFIED")
    if restore:
        needed = ("I_UNDERSTAND_THIS_RESTORES", "RUN_REVIEWED")
    if not authorized or tuple(confirmations) != needed:
        raise ApplyNotAuthorized(
            "not_authorized", "restore" if restore else "apply")


def _completed_run(db: Session, plan_hash: str) -> HistoryRemediationRun | None:
    from cestaplan_api.models import HistoryRemediationRun
    return db.execute(select(HistoryRemediationRun).where(
        HistoryRemediationRun.plan_hash == plan_hash,
        HistoryRemediationRun.status == "applied")).scalar_one_or_none()


def _json(state: dict[str, Any]) -> dict[str, Any]:
    return _norm_state(state)


def _value_execution_hash(plan_hash: str, changes: list[dict[str, Any]]) -> str:
    return planner._value_hash({"plan_hash": plan_hash, "changes": sorted(
        (c["price_observation_id"], c["action_type"], c["expected_bound_hash"]) for c in changes)})


def _assert_counts_preserved(before: dict[str, int], after: dict[str, int]) -> None:
    if before["price_observation"] != after["price_observation"] or \
            before["price_observation_occurrence"] != after["price_observation_occurrence"]:
        raise ApplyPlanDrift("counts_not_preserved",
                             f"{before} != {after}")  # facts/occurrences must never change count


# --------------------------------------------------------------------------- #
# CLI — production allows ONLY --verify-only (spec §12)
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:  # pragma: no cover - thin CLI wrapper
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--manifest-path", required=True)
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--verify-only", action="store_true")
    mode.add_argument("--simulate", action="store_true")
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--restore", metavar="RUN_PUBLIC_ID")
    a = p.parse_args(argv)
    manifest = load_manifest(a.manifest_path)
    ctx = ApplyContext.from_environment(now=datetime.now(UTC))
    if a.apply or a.restore:
        raise SystemExit(
            "ABORT: --apply/--restore are not authorized in this phase. Only --verify-only "
            "(read-only) may run against production.")
    with SessionLocal() as db:
        if a.verify_only:
            db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
            out = verify_only(db, manifest, ctx)
        else:
            out = simulate(db, manifest, ctx)
        db.rollback()
    json.dump(out, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
