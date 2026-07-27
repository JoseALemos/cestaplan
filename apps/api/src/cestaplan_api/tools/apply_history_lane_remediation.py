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
import contextvars
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import event, func, inspect, select, text
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
    from cestaplan_api.models import HistoryRemediationChange, HistoryRemediationRun

APPLY_TOOL_VERSION = "0.1.0-apply"
REQUIRED_SCHEMA_VERSION = 4
# Version of the canonical apply/run evidence seal (spec §1v5/§2v5). Bump only on a contract change.
EVIDENCE_SCHEMA_VERSION = 2  # v2 adds sealed authorization identity + expected backup to the seal
REQUIRED_PLANNER_TOOL_VERSION = "0.4.0-plan-only"
# Fixed, compiled runtime paths (spec §2v3): in cloud/production these are the ONLY provenance +
# trust-root locations; a mutable BUILD_PROVENANCE_PATH / _TRUST_ROOT_PATH env var
# cannot redirect them. (The signed AUTHORIZATION_PACKAGE_PATH / _SIGNATURE_PATH stay configurable.)
RUNTIME_BUILD_PROVENANCE_PATH = "/app/build-provenance.json"
RUNTIME_AUTHORIZATION_TRUST_ROOT_PATH = "/app/authorization-trust-root.json"
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
# Apply v1 scope (§11): same-timestamp DISPUTED marking is deliberately OUT — those conflicts need a
# separate, future review. keep/excluded_no_action are inert; only the two below actually write.
_SUPPORTED_ACTIONS = frozenset({
    "keep", "excluded_no_action", "logical_rollback_exact_duplicate", "reconstruct_interval",
})
_ACTION_WRITES = frozenset({"logical_rollback_exact_duplicate", "reconstruct_interval"})
_V1_BLOCKED_ACTIONS = frozenset({"mark_disputed_same_timestamp_conflict"})
# create_price_anomaly side effects: only these types/severities are allowlisted for apply v1.
_ALLOWED_ANOMALY_TYPES = frozenset({planner._SAME_TIMESTAMP_CONFLICT})
_ALLOWED_ANOMALY_SEVERITIES = frozenset({"high", "medium", "low"})
# Full 64-hex sha256 / 40-hex git commit validators for provenance evidence (§1).
_SHA256_RE = re.compile(r"^(?:sha256:)?[0-9a-f]{64}$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
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
        self.failed_run_id: str | None = None  # set to the durable failed-run public_id (§2)
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


class ApplySessionNotClean(ApplyError):
    """apply/restore got a session with pending new/dirty/deleted state (spec §10)."""


# --------------------------------------------------------------------------- #
# Immutable build provenance — exact expected/observed comparison (spec §1/§12)
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class BuildProvenance:
    """IMMUTABLE, build-time evidence read from a provenance document baked into the image. Never
    from mutable per-service runtime vars alone (spec §1)."""

    commit_sha: str | None = None
    source_tree_hash: str | None = None
    api_artifact_hash: str | None = None
    worker_artifact_hash: str | None = None
    document_hash: str | None = None  # sha256 of the provenance document file itself
    alembic_revision: str | None = None
    generator_version: str | None = None
    authorization_trust_root_hash: str | None = None  # trust-root hash recorded in the document


@dataclass(slots=True)
class ExpectedProvenance:
    """The EXPECTED evidence, from a separately reviewed and sealed authorization package — never
    the same runtime variables that supply the observed values (spec §1)."""

    commit_sha: str | None = None
    source_tree_hash: str | None = None
    api_artifact_hash: str | None = None
    worker_artifact_hash: str | None = None
    document_hash: str | None = None


def _is_cloud() -> bool:
    return os.environ.get("DEPLOYMENT_MODE", "").lower() in ("cloud", "production")


def _runtime_provenance_paths() -> tuple[str | None, str | None]:
    """(build_provenance_path, trust_root_path) for the CURRENT deployment (spec §2v3). In
    cloud/production the FIXED baked paths are used and any BUILD_PROVENANCE_PATH /
    BUILD_AUTHORIZATION_TRUST_ROOT_PATH env override is IGNORED; elsewhere (self_hosted / tests) the
    env may point at temp files via the explicit internal path."""
    if _is_cloud():
        return RUNTIME_BUILD_PROVENANCE_PATH, RUNTIME_AUTHORIZATION_TRUST_ROOT_PATH
    return (os.environ.get("BUILD_PROVENANCE_PATH"),
            os.environ.get("BUILD_AUTHORIZATION_TRUST_ROOT_PATH"))


def load_build_provenance(path: str | None = None) -> BuildProvenance:
    """Read the build-generated provenance document (a JSON file baked into the image) and hash it.

    A missing/unreadable/malformed document yields an empty BuildProvenance — every gate then fails
    closed. Runtime env vars alone are NOT accepted as immutable evidence.
    """
    path = path or os.environ.get("BUILD_PROVENANCE_PATH")
    if not path or not Path(path).is_file():
        return BuildProvenance()
    try:
        raw = Path(path).read_bytes()
        doc = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        return BuildProvenance()
    from cestaplan_api.provenance.generator import (
        EVIDENCE_DOCUMENT_SCHEMA_VERSION,
        GENERATOR_VERSION,
        PYTHON_BASE_IMAGE_DIGEST,
        TOOLCHAIN_CONTRACT_VERSION,
        UV_IMAGE_DIGEST,
        render_document,
    )
    # STRICT (§3v2/§7v3): exact fields, exact schema/generator/toolchain-contract, the python + uv
    # digests EXACTLY equal to the reviewed constants, valid commit/hashes/revision, and bytes ==
    # render_document(doc). Any deviation fails closed (empty BuildProvenance) even if the field is
    # present and well-formed (a canonical document with a different python/uv digest is rejected).
    required = {"schema_version", "commit_sha", "source_tree_hash", "api_artifact_hash",
                "worker_artifact_hash", "alembic_revision", "generator_version",
                "toolchain_contract_version", "python_base_image_digest", "uv_image_digest",
                "authorization_trust_root_hash"}
    if not isinstance(doc, dict) or set(doc) != required:
        return BuildProvenance()
    if (doc["schema_version"] != EVIDENCE_DOCUMENT_SCHEMA_VERSION
            or doc["generator_version"] != GENERATOR_VERSION
            or doc["toolchain_contract_version"] != TOOLCHAIN_CONTRACT_VERSION
            or doc["python_base_image_digest"] != PYTHON_BASE_IMAGE_DIGEST
            or doc["uv_image_digest"] != UV_IMAGE_DIGEST
            or not _valid_commit(doc["commit_sha"])
            or not all(_valid_sha256(doc[k]) for k in (
                "source_tree_hash", "api_artifact_hash", "worker_artifact_hash",
                "authorization_trust_root_hash"))
            or not (isinstance(doc["alembic_revision"], str)
                    and bool(re.match(r"^[0-9a-z_]{6,64}$", doc["alembic_revision"])))
            or render_document(doc) != raw):
        return BuildProvenance()
    return BuildProvenance(
        commit_sha=doc["commit_sha"], source_tree_hash=doc["source_tree_hash"],
        api_artifact_hash=doc["api_artifact_hash"],
        worker_artifact_hash=doc["worker_artifact_hash"],
        document_hash=hashlib.sha256(raw).hexdigest(),
        alembic_revision=doc["alembic_revision"], generator_version=doc["generator_version"],
        authorization_trust_root_hash=doc["authorization_trust_root_hash"])


def _valid_sha256(v: str | None) -> bool:
    return isinstance(v, str) and bool(_SHA256_RE.match(v))


def _valid_commit(v: str | None) -> bool:
    return isinstance(v, str) and bool(_COMMIT_RE.match(v))


# --------------------------------------------------------------------------- #
# Real backup evidence (spec §9)
# --------------------------------------------------------------------------- #
_BACKUP_MAX_AGE_SECONDS = 6 * 3600


def _major(version: str | None) -> str | None:
    """Extract the PostgreSQL major (e.g. '16' from 'pg_restore (PostgreSQL) 16.2')."""
    if not version:
        return None
    m = re.search(r"(\d+)(?:\.\d+)*", str(version))
    return m.group(1) if m else None


def _stream_sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _dump_db_version(restore_list: str | None) -> str | None:
    if not restore_list:
        return None
    m = re.search(r"database version[:\s]+(\d+)", restore_list, re.IGNORECASE)
    return m.group(1) if m else None


# A storage reference is either an opaque allowlisted id OR a bare bucket URI. Anything that could
# carry a credential, token or signed-URL parameter is rejected outright (spec §4v4).
_STORAGE_REF_MAX_LEN = 200
_STORAGE_REF_SCHEMES = frozenset({"s3", "gs", "gcs", "b2"})
# Opaque ids admit NO path separators or colons (spec §4v5): a hierarchy MUST use an allowlisted
# URI, so a disguised local/UNC/drive path (C:/…, C:\…, backups/…, ./…, \\host\…) can never pass.
_STORAGE_OPAQUE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
_STORAGE_PATH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-/]*$")
_STORAGE_SENSITIVE_RE = re.compile(
    r"(?i)(token|secret|signature|sig=|credential|password|passwd|x-amz-|access[_-]?key|api[_-]?key)")


def sanitize_storage_reference(ref: str | None) -> str | None:
    """Return a SAFE-to-persist reference or ``None`` when it cannot be sanitized (spec §4v4).

    Never returns anything containing userinfo, a query string, a fragment, a sensitive parameter,
    a control character or an over-long value. A local filesystem path is not a valid reference.
    """
    if not isinstance(ref, str) or not (0 < len(ref) <= _STORAGE_REF_MAX_LEN):
        return None
    if any(ord(c) < 0x20 or ord(c) == 0x7f for c in ref):  # control chars, tabs, newlines
        return None
    if _STORAGE_SENSITIVE_RE.search(ref) or "@" in ref:
        return None
    if "://" not in ref:  # opaque identifier
        if ref.startswith(("/", "./", "../")):  # a local path is not an opaque reference
            return None
        return ref if _STORAGE_OPAQUE_RE.match(ref) else None
    try:
        parsed = urllib.parse.urlsplit(ref)
    except ValueError:
        return None
    if parsed.scheme.lower() not in _STORAGE_REF_SCHEMES:
        return None
    if parsed.query or parsed.fragment or parsed.username or parsed.password or parsed.port:
        return None
    host_path = (parsed.netloc or "") + (parsed.path or "")
    if not host_path or not _STORAGE_PATH_RE.match(host_path):
        return None
    return ref


def _storage_reference_hash(sanitized: str | None) -> str | None:
    return hashlib.sha256(sanitized.encode()).hexdigest() if sanitized else None


@dataclass(slots=True)
class BackupEvidence:
    """A verified pre-apply backup. ``verify()`` checks the artifact on disk — never a bare
    boolean (spec §9)."""

    # §4v5: the local dump path is NEVER rendered (repr=False) — it must not leak into logs, a
    # context repr, a report or a signed package.
    path: str = field(repr=False)
    expected_sha256: str
    created_at: datetime
    expected_postgres_version: str | None = None  # the server major we require compatibility with
    storage_reference: str | None = None  # sanitized

    def verify(self, now: datetime, *,
               server_version: str | None = None) -> tuple[bool, dict[str, Any]]:
        from cestaplan_api.provenance.operational_evidence import (
            PROC_SELF_FD,
            CeremonyFileError,
            secure_open_dump,
            stat_identity,
            stream_sha256_fd,
        )
        ev: dict[str, Any] = {
            "path_present": False, "regular_file": False, "permissions_not_public": False,
            "size_positive": False, "sha256_matches": False, "pg_restore_list_verified": False,
            "identity_stable": False,
            "within_window": False, "compatibility_ok": False, "reference_sanitized": True,
            "size_bytes": 0, "observed_sha256": None, "expected_postgres_version": _major(
                self.expected_postgres_version), "observed_pg_restore_version": None,
            "observed_database_version": _major(server_version), "dump_database_version": None,
            "storage_reference_sanitized": None, "storage_reference_hash": None}
        # A reference that is present but not sanitizable blocks the backup entirely (§4v4).
        if self.storage_reference is not None:
            _san = sanitize_storage_reference(self.storage_reference)
            ev["reference_sanitized"] = _san is not None
            ev["storage_reference_sanitized"] = _san
            ev["storage_reference_hash"] = _storage_reference_hash(_san)
        # §2v2: stat, hash AND pg_restore all act on ONE securely-opened inode (no symlink
        # component, O_NOFOLLOW); a substitution/truncation/in-place edit at any point fails closed.
        # The path is never placed in ev or in an error.
        try:
            dump = secure_open_dump(self.path)
        except CeremonyFileError:
            return False, ev
        try:
            st0 = os.fstat(dump.fd)
            ev["path_present"] = True
            ev["regular_file"] = True  # secure_open_dump enforced regular + positive size + owner
            ev["permissions_not_public"] = True
            ev["size_bytes"] = st0.st_size
            ev["size_positive"] = st0.st_size > 0
            ev["observed_sha256"] = stream_sha256_fd(dump.fd)  # from the held fd, streaming
            ev["sha256_matches"] = _valid_sha256(self.expected_sha256) and \
                ev["observed_sha256"] == self.expected_sha256.removeprefix("sha256:")
            try:
                # errors="replace": a corrupt/adversarial dump may make pg_restore emit non-UTF-8
                # bytes; decoding must never raise — the verify fails closed on returncode/identity.
                ver = subprocess.run(["pg_restore", "--version"], capture_output=True, text=True,
                                     errors="replace", timeout=30, check=False)
                ev["observed_pg_restore_version"] = _major(ver.stdout.strip()) \
                    if ver.returncode == 0 else None
                if not os.path.isdir(PROC_SELF_FD):
                    ev["pg_restore_list_verified"] = False  # cannot pass the fd -> fail closed
                else:
                    os.lseek(dump.fd, 0, os.SEEK_SET)
                    lst = subprocess.run(  # examine the SAME descriptor, not a re-opened path
                        ["pg_restore", "--list", f"{PROC_SELF_FD}/{dump.fd}"],
                        pass_fds=(dump.fd,), capture_output=True, text=True, errors="replace",
                        timeout=60, check=False)
                    ev["pg_restore_list_verified"] = lst.returncode == 0 and bool(lst.stdout)
                    ev["dump_database_version"] = _dump_db_version(lst.stdout)
            except (OSError, ValueError, subprocess.SubprocessError):
                ev["pg_restore_list_verified"] = False
            st1 = os.fstat(dump.fd)
            after = dump.reopen_stat()  # descriptor-relative re-open; require identical identity
            ev["identity_stable"] = (
                stat_identity(st0) == stat_identity(st1) == stat_identity(after))
        finally:
            dump.close()
        # Compatibility is EXPLICIT: dump/database/pg_restore majors must all agree with expected.
        majors = {ev["expected_postgres_version"], ev["observed_database_version"],
                  ev["dump_database_version"], ev["observed_pg_restore_version"]}
        majors.discard(None)
        ev["compatibility_ok"] = len(majors) == 1 and ev["expected_postgres_version"] is not None
        age = (now - self.created_at).total_seconds()
        ev["within_window"] = 0 <= age <= _BACKUP_MAX_AGE_SECONDS
        ok = all(ev[k] for k in (
            "path_present", "regular_file", "permissions_not_public", "size_positive",
            "sha256_matches", "pg_restore_list_verified", "identity_stable", "within_window",
            "compatibility_ok", "reference_sanitized"))
        return ok, ev


@dataclass(slots=True)
class ApplyContext:
    """What the executor needs beyond the DB session and manifest (injected, never guessed)."""

    app_commit_sha: str | None = None
    deployed_api_sha: str | None = None
    deployed_worker_sha: str | None = None
    expected_main_sha: str | None = None
    expected_alembic: str | None = None
    observed_provenance: BuildProvenance = field(default_factory=BuildProvenance)
    expected_provenance: ExpectedProvenance = field(default_factory=ExpectedProvenance)
    # ProductPrice / active mappings expected counts — 0 unless a future manifest authorizes a
    # change (spec §5). The gate compares the live count to these, never a hardcoded 0.
    expected_product_price: int = 0
    expected_active_mappings: int = 0
    backup: BackupEvidence | None = None
    operator_reference: str | None = None
    now: datetime | None = None
    # Sanitized status of the sealed authorization package (set by from_environment); EXPECTED
    # come from that package, never from the same runtime env that supplies the OBSERVED values.
    authorization: dict[str, Any] | None = None
    # --- Authorization identity + expected backup, filled ONLY from the signed package (§1v2) ---
    authorization_id: str | None = None
    authorization_package_hash: str | None = None
    authorization_key_fingerprint: str | None = None
    authorization_generated_at: datetime | None = None
    authorization_expires_at: datetime | None = None
    expected_backup_sha256: str | None = None
    expected_backup_storage_reference: str | None = None
    expected_backup_storage_reference_hash: str | None = None
    # --- Explicit authorization gate (§3v3): only the verified loader sets these operationally ---
    authorization_plan_hash: str | None = None
    authorization_validated_at: datetime | None = None
    authorization_valid: bool = False
    # --- Ceremony-pinned file paths (§6v5): the EXACT package/signature/trust-root files the
    # under-lock revalidation must re-read, so a later env change cannot redirect it. NEVER rendered
    # (repr=False) or persisted — a local path must not leak into logs, reports or audit. ---
    _ceremony_package_path: str | None = field(default=None, repr=False)
    _ceremony_signature_path: str | None = field(default=None, repr=False)
    _ceremony_trust_root_path: str | None = field(default=None, repr=False)

    # These are OWNED by the signed package on the operational path — a caller may not override them
    # via from_environment (tests inject them by building ApplyContext(...) directly).
    _PACKAGE_OWNED = frozenset({
        "expected_provenance", "expected_main_sha", "expected_alembic", "expected_product_price",
        "expected_active_mappings", "operator_reference", "authorization_id",
        "authorization_package_hash", "authorization_key_fingerprint", "authorization_generated_at",
        "authorization_expires_at", "expected_backup_sha256", "expected_backup_storage_reference",
        "expected_backup_storage_reference_hash", "authorization_plan_hash",
        "authorization_validated_at", "authorization_valid"})

    @classmethod
    def from_environment(cls, *, plan_hash: str | None = None, **overrides: Any) -> ApplyContext:
        forbidden = set(overrides) & cls._PACKAGE_OWNED
        if forbidden:
            raise ApplyNotAuthorized(
                "override_forbidden_operational_path", ",".join(sorted(forbidden)))
        # §1v4: in cloud/production NO caller may inject the clock — the only operational time
        # source is _now_utc(), captured under the global lock. A "now" override is a test-only
        # affordance (self_hosted) and is rejected outright in cloud.
        if "now" in overrides and _is_cloud():
            raise ApplyNotAuthorized("override_forbidden_operational_clock", "now")
        bp_path, _tr_path = _runtime_provenance_paths()  # fixed paths in cloud/production (§2v3)
        base = cls(
            app_commit_sha=os.environ.get("APP_COMMIT_SHA"),
            deployed_api_sha=os.environ.get("DEPLOYED_API_SHA") or os.environ.get("APP_COMMIT_SHA"),
            deployed_worker_sha=os.environ.get("DEPLOYED_WORKER_SHA"),
            observed_provenance=load_build_provenance(bp_path))
        for k, v in overrides.items():
            setattr(base, k, v)
        # Pre-check load clock only (self_hosted tests may inject base.now); the OPERATIONAL
        # temporal decision is re-made under the lock with operation_now. Cloud always _now_utc().
        now = base.now or _now_utc()
        # The sealed package (verified against the FIXED baked trust-root) is the ONLY source of
        # expected values + the authorization identity (§3v3).
        status, pkg = _load_authorization(plan_hash, now)
        base.authorization = status
        if pkg is not None:
            _apply_package_to_context(base, pkg, now)
        return base

    @classmethod
    def from_ceremony_files(
            cls, *, plan_hash: str, operational_evidence_path: str,
            authorization_package_path: str | None = None,
            authorization_signature_path: str | None = None) -> ApplyContext:
        """Build the context for the verify-only authorization ceremony (§5v5) from explicit files.

        The build-provenance + trust-root come from the FIXED baked paths; the BackupEvidence and
        deployed API/worker commits come from the OBSERVED operational-evidence file (never a source
        of expected values); APP_COMMIT_SHA stays an independent observed fact. The signed package
        (if supplied) is loaded ONLY through the verified loader against the EXACT pinned files, and
        is the sole source of every expected value + the authorization identity. No package-owned
        field can be overridden. The exact package/signature/trust-root paths are pinned on the
        context so the under-lock revalidation re-reads those same files, not the environment."""
        from cestaplan_api.provenance.operational_evidence import load_operational_evidence
        # §4v2: normalize every ceremony path to an ABSOLUTE path ONCE; the same absolute strings
        # are pinned on the context and used by the under-lock revalidation, so nothing drifts.
        evidence_abs = os.path.abspath(operational_evidence_path)
        pkg_abs = os.path.abspath(authorization_package_path) \
            if authorization_package_path is not None else None
        sig_abs = os.path.abspath(authorization_signature_path) \
            if authorization_signature_path is not None else None
        ev = load_operational_evidence(evidence_abs)
        bp_path, tr_path = _runtime_provenance_paths()
        tr_abs = os.path.abspath(tr_path) if tr_path else tr_path
        base = cls(
            app_commit_sha=os.environ.get("APP_COMMIT_SHA"),  # independent OBSERVED fact
            deployed_api_sha=ev.deployed_api_sha,
            deployed_worker_sha=ev.deployed_worker_sha,
            observed_provenance=load_build_provenance(bp_path),
            backup=BackupEvidence(
                path=ev.backup_path, expected_sha256=ev.backup_expected_sha256,
                created_at=ev.backup_created_at,
                expected_postgres_version=ev.backup_expected_postgres_version,
                storage_reference=ev.backup_storage_reference))
        base._ceremony_trust_root_path = tr_abs
        now = _now_utc()
        status, pkg = _load_authorization(
            plan_hash, now, package_path=pkg_abs, signature_path=sig_abs,
            trust_root_path=tr_abs, use_env_paths=False)
        base.authorization = status
        if pkg_abs is not None and sig_abs is not None:
            base._ceremony_package_path = pkg_abs
            base._ceremony_signature_path = sig_abs
        if pkg is not None:
            _apply_package_to_context(base, pkg, now)
        return base


def _load_authorization(plan_hash: str | None, now: datetime, *,
                        package_path: str | None = None, signature_path: str | None = None,
                        trust_root_path: str | None = None, use_env_paths: bool = True,
                        trust_root_keys: list[str] | None = None) -> tuple[dict[str, Any], Any]:
    """Load + verify the sealed authorization package (feat provenance v2/v5). The authorized keys
    come ONLY from a BAKED trust-root file (never a runtime env var). Explicit ``package_path`` /
    ``signature_path`` / ``trust_root_path`` pin the EXACT files (used by the ceremony context so a
    later env change can never redirect the under-lock revalidation, §6v5); ``use_env_paths`` keeps
    ``from_environment`` backward-compatible. Package + signature are read fail-closed (O_NOFOLLOW,
    regular-file, race-safe). ``trust_root_keys`` is a test hook. No package/plan_hash -> absent;
    any failure -> sanitized status + None, so expected provenance stays empty and apply_ready
    false."""
    from cestaplan_api.provenance import (
        AuthorizationError,
        TrustRootError,
        load_authorization_package,
        load_trust_root,
    )
    from cestaplan_api.provenance.operational_evidence import (
        MAX_PACKAGE_BYTES,
        MAX_SIGNATURE_BYTES,
        CeremonyFileError,
        secure_read_bytes,
    )
    if package_path is None and use_env_paths:
        package_path = os.environ.get("AUTHORIZATION_PACKAGE_PATH")
    if signature_path is None and use_env_paths:
        signature_path = os.environ.get("AUTHORIZATION_SIGNATURE_PATH")
    if not package_path or not signature_path or plan_hash is None:
        return {"package_present": False}, None
    if trust_root_keys is not None:
        keys = trust_root_keys
    else:
        trust_path = trust_root_path if trust_root_path is not None \
            else _runtime_provenance_paths()[1]  # fixed in cloud/production (§2v3)
        if not trust_path or not Path(trust_path).is_file():
            return {"package_present": True, "signature_valid": False,
                    "error_code": "trust_root_missing"}, None
        try:
            keys = load_trust_root(trust_path)
        except TrustRootError as exc:
            return {"package_present": True, "signature_valid": False,
                    "error_code": exc.code}, None
    if not os.path.lexists(package_path) or not os.path.lexists(signature_path):
        return {"package_present": False, "error_code": "package_files_missing"}, None
    try:  # fail-closed, symlink-rejecting, race-safe, size-capped reads of the pinned files (§10v5)
        pkg_bytes = secure_read_bytes(package_path, max_bytes=MAX_PACKAGE_BYTES)
        sig_text = secure_read_bytes(
            signature_path, max_bytes=MAX_SIGNATURE_BYTES).decode("utf-8").strip()
    except CeremonyFileError as exc:
        return {"package_present": True, "signature_valid": False, "error_code": exc.code}, None
    except (OSError, UnicodeDecodeError):
        return {"package_present": True, "signature_valid": False,
                "error_code": "package_unreadable"}, None
    try:
        pkg = load_authorization_package(
            pkg_bytes, sig_text, authorized_public_keys=keys, now=now, expected_plan_hash=plan_hash)
    except AuthorizationError as exc:
        sig_codes = {"signature_not_authorized", "signature_malformed", "no_authorized_public_keys"}
        return {"package_present": True, "signature_valid": exc.code not in sig_codes,
                "expired": exc.code == "package_expired", "error_code": exc.code}, None
    return {"package_present": True, "signature_valid": True, "expired": False,
            "authorization_id": pkg.authorization_id,
            "key_fingerprint": pkg.public_key_fingerprint}, pkg


def _apply_package_to_context(base: ApplyContext, pkg: Any, now: datetime) -> None:
    """Copy the EXPECTED values + authorization identity from a verified package into the context.
    The signed package is the ONLY source of these (§3v3) — shared by from_environment + ceremony.
    """
    base.expected_provenance = ExpectedProvenance(**pkg.expected_provenance_fields())
    base.expected_main_sha = pkg.main_commit_sha
    base.expected_alembic = pkg.alembic_revision
    base.expected_product_price = pkg.expected_product_price
    base.expected_active_mappings = pkg.expected_active_mappings
    base.operator_reference = pkg.operator_reference
    base.authorization_id = pkg.authorization_id
    base.authorization_package_hash = pkg.authorization_package_hash
    base.authorization_key_fingerprint = pkg.public_key_fingerprint
    base.authorization_generated_at = pkg.generated_at
    base.authorization_expires_at = pkg.expires_at
    base.expected_backup_sha256 = pkg.backup_expected_sha256
    base.expected_backup_storage_reference = pkg.backup_storage_reference
    base.expected_backup_storage_reference_hash = pkg.backup_storage_reference_hash
    base.authorization_plan_hash = pkg.plan_hash
    base.authorization_validated_at = now
    base.authorization_valid = True


def _document_toolchain_ok() -> dict[str, bool]:
    """Sanitized booleans for the document toolchain fields vs the reviewed constants + live
    trust-root (no hashes shown, §7v3); a different python/uv digest reads False."""
    from cestaplan_api.provenance.generator import (
        GENERATOR_VERSION,
        PYTHON_BASE_IMAGE_DIGEST,
        TOOLCHAIN_CONTRACT_VERSION,
        UV_IMAGE_DIGEST,
    )
    bp_path = _runtime_provenance_paths()[0]
    doc: dict[str, Any] = {}
    if bp_path and Path(bp_path).is_file():
        try:
            loaded = json.loads(Path(bp_path).read_bytes())
            doc = loaded if isinstance(loaded, dict) else {}
        except (OSError, json.JSONDecodeError, ValueError):
            doc = {}
    live_trust = _observed_trust_root_hash()
    return {
        "generator_version_match": doc.get("generator_version") == GENERATOR_VERSION,
        "toolchain_contract_match": doc.get("toolchain_contract_version")
        == TOOLCHAIN_CONTRACT_VERSION,
        "python_base_image_digest_match": doc.get("python_base_image_digest")
        == PYTHON_BASE_IMAGE_DIGEST,
        "uv_image_digest_match": doc.get("uv_image_digest") == UV_IMAGE_DIGEST,
        "trust_root_live_match": _valid_sha256(live_trust)
        and live_trust == doc.get("authorization_trust_root_hash"),
    }


def _provenance_report(m: dict[str, Any], ctx: ApplyContext) -> dict[str, Any]:
    """Sanitized provenance status for verify-only (no hashes leaked — only match booleans)."""
    gates = dict(_provenance_gates(m, ctx))
    ident = dict(_build_identity_gates_safe(ctx))
    o = ctx.observed_provenance
    return {
        "document_found": o.document_hash is not None,
        "schema_valid": o.document_hash is not None,  # load_build_provenance rejects other schemas
        "commit_present": _valid_commit(o.commit_sha),
        "source_tree_match": gates.get("provenance_source_matches", False),
        "api_artifact_match": gates.get("provenance_api_artifact_matches", False),
        "worker_artifact_match": gates.get("provenance_worker_artifact_matches", False),
        "document_match": gates.get("provenance_document_matches", False),
        "immutable_build_provenance": gates.get("immutable_build_provenance", False),
        "trust_root_match": ident.get("trust_root_matches_document", False),
        **_document_toolchain_ok(),
    }


def _build_identity_gates_safe(ctx: ApplyContext) -> list[tuple[str, bool]]:
    """Build-identity gates that need no DB session (for the sanitized verify-only report)."""
    o, e = ctx.observed_provenance, ctx.expected_provenance
    return [
        ("build_doc_commit_matches_app", _eq_commit(o.commit_sha, ctx.app_commit_sha)),
        ("package_main_matches_expected", _eq_commit(ctx.expected_main_sha, e.commit_sha)),
        ("trust_root_matches_document", _valid_sha256(_observed_trust_root_hash())
         and _observed_trust_root_hash() == o.authorization_trust_root_hash),
    ]


def _authorization_report(ctx: ApplyContext) -> dict[str, Any]:
    a = ctx.authorization or {"package_present": False}
    return {
        "package_present": a.get("package_present", False),
        "signature_valid": a.get("signature_valid", False),
        "expired": a.get("expired", False),
        "error_code": a.get("error_code"),
    }


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
def _eq_sha(a: str | None, b: str | None) -> bool:
    """True only when both are valid 64-hex sha256 and exactly equal (no bare truthiness)."""
    return _valid_sha256(a) and _valid_sha256(b) and \
        a.removeprefix("sha256:") == b.removeprefix("sha256:")  # type: ignore[union-attr]


def _eq_commit(a: str | None, b: str | None) -> bool:
    return _valid_commit(a) and _valid_commit(b) and a == b


def _provenance_gates(m: dict[str, Any], ctx: ApplyContext) -> list[tuple[str, bool]]:
    app = ctx.app_commit_sha
    api = ctx.deployed_api_sha
    worker = ctx.deployed_worker_sha
    expected_main = ctx.expected_main_sha or m["commit_provenance"].get("base_main_sha")
    o, e = ctx.observed_provenance, ctx.expected_provenance
    field_gates = [
        ("provenance_document_present", _valid_sha256(o.document_hash)),
        ("provenance_document_matches", _eq_sha(o.document_hash, e.document_hash)),
        ("provenance_commit_matches", _eq_commit(o.commit_sha, e.commit_sha)),
        ("provenance_source_matches", _eq_sha(o.source_tree_hash, e.source_tree_hash)),
        ("provenance_api_artifact_matches", _eq_sha(o.api_artifact_hash, e.api_artifact_hash)),
        ("provenance_worker_artifact_matches",
         _eq_sha(o.worker_artifact_hash, e.worker_artifact_hash)),
    ]
    immutable_ok = all(ok for _, ok in field_gates)
    return [
        ("app_commit_sha_present", _valid_commit(app)),
        ("api_worker_aligned", _valid_commit(app) and api == app and worker == app),
        ("main_commit_sha_matches", _eq_commit(app, expected_main)),
        ("immutable_build_provenance", immutable_ok),
        *field_gates,
    ]


def _observed_trust_root_hash() -> str | None:
    """SHA-256 of the trust-root file at the RESOLVED path (fixed in cloud/production, §2v3)."""
    path = _runtime_provenance_paths()[1]
    if not path or not Path(path).is_file():
        return None
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


def _build_identity_gates(db: Session, ctx: ApplyContext) -> list[tuple[str, bool]]:
    """Bind the full build identity (spec §3v2): the observed document commit, APP_COMMIT_SHA, the
    package commit/main, the live + document Alembic revision and the trust-root must all agree — no
    separately-coherent-but-contradictory chains. Needs a valid package (empty expected=False)."""
    o, e = ctx.observed_provenance, ctx.expected_provenance
    app = ctx.app_commit_sha
    live_trust = _observed_trust_root_hash()
    return [
        ("build_doc_commit_matches_app", _eq_commit(o.commit_sha, app)),
        ("package_main_matches_expected", _eq_commit(ctx.expected_main_sha, e.commit_sha)),
        ("build_doc_alembic_matches_package",
         o.alembic_revision is not None and o.alembic_revision == ctx.expected_alembic),
        ("build_doc_alembic_matches_live",
         o.alembic_revision is not None and o.alembic_revision == _alembic_revision(db)),
        ("trust_root_matches_document",
         _valid_sha256(live_trust) and live_trust == o.authorization_trust_root_hash),
    ]


def _server_version(db: Session) -> str | None:
    try:
        return db.execute(text("SHOW server_version")).scalar()
    except Exception:
        return None


def _backup_gate(
    db: Session, ctx: ApplyContext, operation_now: datetime
) -> tuple[bool, dict[str, Any]]:
    # §1v4: the single operational clock captured under the global lock is the only temporal
    # source for the backup-age decision — never ctx.now.
    if ctx.backup is None:
        return False, {"backup_present": False}
    ok, ev = ctx.backup.verify(operation_now, server_version=_server_version(db))
    # §1v2: the backup must be the one the SIGNED package authorized — same expected SHA (and the
    # observed file SHA equals it), same sanitized storage reference and same reference hash.
    bound = (
        _valid_sha256(ctx.expected_backup_sha256)
        and ctx.backup.expected_sha256 == ctx.expected_backup_sha256
        and ev.get("observed_sha256") == ctx.expected_backup_sha256
        and ctx.expected_backup_storage_reference is not None
        and ev.get("storage_reference_sanitized") == ctx.expected_backup_storage_reference
        and ev.get("storage_reference_hash") == ctx.expected_backup_storage_reference_hash)
    ev["authorization_backup_bound"] = bound
    return (ok and bound), ev


# --------------------------------------------------------------------------- #
# Manifest apply_blockers resolution policy (spec §2)
# --------------------------------------------------------------------------- #
_KNOWN_BLOCKERS = frozenset({
    "planner_is_plan_only", "record_price_fact_rolled_back_reuse_not_remediated",
    "unknown_commit_provenance",
})


def _full_provenance_ok(db: Session, m: dict[str, Any], ctx: ApplyContext, *,
                        now: datetime) -> bool:
    """The conjunction of EVERY provenance, build-identity AND authorization gate (§4v3): app-commit
    present, api/worker aligned, main-commit match, immutable build, the full build identity (doc
    commit == APP == package main == expected; doc Alembic == package Alembic == live DB; trust-root
    live == document) and a valid, current, plan-bound authorization package. A manifest blocker is
    never 'resolved' while any of these fails — so the report never shows a blocker resolved while
    build identity or authorization is blocking."""
    return (all(ok for _, ok in _provenance_gates(m, ctx))
            and all(ok for _, ok in _build_identity_gates(db, ctx))
            and all(ok for _, ok in _authorization_gates(m, ctx, now)))


def _resolve_manifest_blockers(m: dict[str, Any], *, writer_ok: bool,
                               full_provenance_ok: bool) -> tuple[list[str], list[str], list[str]]:
    """Classify each sealed manifest blocker (§2/§8). An unknown blocker, or a known one not PROVEN
    resolved (via FULL provenance, not merely immutable_build_provenance), stays unresolved."""
    present = list(m.get("apply_blockers", []))
    resolved: list[str] = []
    unresolved: list[str] = []
    for b in present:
        if b == "planner_is_plan_only":
            # Resolved solely because this separate, reviewed executor now exists.
            resolved.append(b)
        elif b == "record_price_fact_rolled_back_reuse_not_remediated":
            (resolved if (writer_ok and full_provenance_ok) else unresolved).append(b)
        elif b == "unknown_commit_provenance":
            (resolved if full_provenance_ok else unresolved).append(b)
        else:
            unresolved.append(b)  # unknown blocker -> fail closed
    return present, resolved, unresolved


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


def _supported_fk_identity(fk: dict[str, Any]) -> tuple:
    return (fk["referencing_schema"], fk["referencing_table"], fk["referencing_column"],
            fk["referred_schema"], fk["referred_table"], fk["referred_column"],
            fk["constraint_name"], fk["pk"], fk["full_row_hash"], fk["apply_policy"],
            fk["restore_policy"])


def _supported_fk_unchanged(db: Session, m: dict[str, Any], discovered, obs_ids) -> bool:
    """Re-derive every SUPPORTED FK dependency live and require the sealed identity set — schema/
    table/column both sides, constraint_name, PK, full_row_hash and policies — matches EXACTLY
    (spec §3). Any added / changed / removed dependency row blocks."""
    if not obs_ids:
        return True
    sealed: dict[int, set] = {r["id"]: {_supported_fk_identity(fk)
                                        for fk in r.get("incoming_fk_state", [])}
                              for r in _planned_rows(m)}
    live: dict[int, set] = {oid: set() for oid in obs_ids}
    for fk in discovered:
        if fk.get("classification") != "domain_supported":
            continue
        handler = planner._FK_HANDLERS.get(planner._fk_key(fk))
        if not handler or not handler["emit"] or handler["model"] is None:
            continue
        model = handler["model"]
        col = getattr(model, fk["referencing_column"])
        for row in db.execute(select(model).where(col.in_(obs_ids))).scalars():
            entry = planner._fk_manifest(fk, model, row)
            live[getattr(row, fk["referencing_column"])].add(_supported_fk_identity(entry))
    return all(sealed.get(oid, set()) == live.get(oid, set()) for oid in obs_ids)


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
    ok_supported_fk = _supported_fk_unchanged(db, m, discovered, obs_ids)
    return [("row_hashes_match", ok_rows), ("occurrences_unchanged", ok_occ),
            ("supported_fk_unchanged", ok_supported_fk),
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
# The ONLY anomaly-DELETE shape allowed: a single primary-key equality with a bound param (§5).
_ANOMALY_DELETE_RE = re.compile(
    r'^\s*delete\s+from\s+(?:only\s+)?"?(?:public\.)?"?price_anomaly"?\s+'
    r'where\s+"?price_anomaly"?\s*\.\s*"?id"?\s*=\s*%\(\w+\)s(?:::\w+)?\s*$',
    re.IGNORECASE)


# Per-instance guard state lives in a ContextVar (thread/async-safe), NEVER a shared module dict.
_guard_state: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "apply_write_guard", default=None)


def _forbid(statement: str, params: Any) -> None:
    stmt = statement.strip()
    mm = _DML_RE.match(stmt)
    if not mm:  # not INSERT/UPDATE/DELETE (SELECT, SET, SHOW, SAVEPOINT, …) -> allowed
        return
    op = mm.group(1).lower().split()[0]
    table = mm.group(2).lower()
    state = _guard_state.get() or {}
    if table in _FORBIDDEN_TABLES:
        raise ApplyForbiddenWrite("forbidden_write_occurrence", table)
    if op == "delete":
        if table != _ANOMALY_TABLE or not state.get("allow_anomaly_delete"):
            raise ApplyForbiddenWrite("forbidden_delete", table)
        # Accept ONLY the exact single-id ORM form: `DELETE FROM price_anomaly WHERE
        # price_anomaly.id = %(param)s`. Any OR/IN/AND/subquery/extra predicate is rejected (§5).
        if not _ANOMALY_DELETE_RE.match(stmt):
            raise ApplyForbiddenWrite("forbidden_delete_shape", table)
        if isinstance(params, (list, tuple)) and len(params) != 1:  # never an executemany batch
            raise ApplyForbiddenWrite("forbidden_delete_batch", table)
        rid = _param_id(params)
        if rid is None or rid not in state.get("allowed_anomaly_ids", frozenset()):
            raise ApplyForbiddenWrite("forbidden_delete_unauthorized_id", str(rid))
        return
    if op == "insert":
        if table in _AUDIT_TABLES or table == _ANOMALY_TABLE:
            return
        raise ApplyForbiddenWrite("forbidden_insert", table)
    if table in _AUDIT_TABLES:  # UPDATE
        return
    if table == "price_observation":
        body = _SET_COLS_RE.search(stmt)
        cols = {c.lower() for c in _COL_RE.findall(body.group(1))} if body else set()
        if cols and cols <= _ALLOWED_PO_UPDATE_COLS:
            return
        raise ApplyForbiddenWrite("forbidden_update_columns",
                                  ",".join(sorted(cols - _ALLOWED_PO_UPDATE_COLS)) or "unparsed")
    raise ApplyForbiddenWrite("forbidden_update", table)


def _param_id(params: Any) -> Any:
    if isinstance(params, dict):
        return params.get("id")
    if isinstance(params, (list, tuple)) and len(params) == 1 and isinstance(params[0], dict):
        return params[0].get("id")
    return None


def _orm_flush_guard(session: Session, _flush_ctx: Any, _instances: Any) -> None:
    """ORM-level defence (§8): a dirty PriceObservation may only touch whitelisted attrs; a fact or
    occurrence may never be deleted; only anomalies/audit rows may be new; anomaly deletes must be
    allowlisted. Complements the connection-scoped SQL interceptor for raw SQL."""
    state = _guard_state.get() or {}
    allowed = state.get("allowed_anomaly_ids", frozenset())
    for obj in session.deleted:
        if isinstance(obj, (PriceObservation, planner.PriceObservationOccurrence)):
            raise ApplyForbiddenWrite("forbidden_orm_delete_fact", type(obj).__name__)
        if isinstance(obj, PriceAnomaly) and (
                not state.get("allow_anomaly_delete") or obj.id not in allowed):
            raise ApplyForbiddenWrite("forbidden_orm_delete_anomaly", str(obj.id))
    for obj in session.new:
        if isinstance(obj, (PriceObservation, planner.PriceObservationOccurrence)):
            raise ApplyForbiddenWrite("forbidden_orm_new_fact", type(obj).__name__)
    for obj in session.dirty:
        if isinstance(obj, PriceObservation):
            changed = {a.key for a in inspect(obj).attrs if a.history.has_changes()}
            if not changed <= _ALLOWED_PO_UPDATE_COLS:
                raise ApplyForbiddenWrite("forbidden_orm_update_columns",
                                          ",".join(sorted(changed - _ALLOWED_PO_UPDATE_COLS)))
        elif isinstance(obj, planner.PriceObservationOccurrence):
            raise ApplyForbiddenWrite("forbidden_orm_update_occurrence", "")


class _WriteGuard:
    """Attach guards that FAIL on any write outside the whitelist, for the duration. The SQL
    interceptor is bound to THIS connection only (never the global engine); an anomaly DELETE is
    limited to an exact allowlist of ids (spec §8)."""

    def __init__(self, db: Session, *, allow_anomaly_delete: bool = False,
                 allowed_anomaly_ids: frozenset[int] = frozenset()) -> None:
        self._db = db
        self._conn = db.connection()
        self._state = {"allow_anomaly_delete": allow_anomaly_delete,
                       "allowed_anomaly_ids": allowed_anomaly_ids}
        self._token: Any = None

    def __enter__(self) -> _WriteGuard:
        self._token = _guard_state.set(self._state)

        def _before(conn, cursor, statement, params, context, executemany):
            _forbid(statement, params)

        self._listener = _before
        event.listen(self._conn, "before_cursor_execute", self._listener)
        event.listen(self._db, "before_flush", _orm_flush_guard)
        return self

    def __exit__(self, *exc: Any) -> None:
        event.remove(self._conn, "before_cursor_execute", self._listener)
        event.remove(self._db, "before_flush", _orm_flush_guard)
        _guard_state.reset(self._token)


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
def _now_utc() -> datetime:
    """A fresh UTC instant. The single internal clock hook — tests monkeypatch THIS; an operational
    caller can never pass an arbitrary clock into the gates (spec §3v3)."""
    return datetime.now(UTC)


_AUTH_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_FINGERPRINT_RE = re.compile(r"^[0-9a-f]{16}$")


def _authorization_revalidated(ctx: ApplyContext, operation_now: datetime) -> bool:
    """§2v4: FULLY re-validate the sealed authorization UNDER the global lock, using operation_now.

    It is not enough to re-read the JSON and compare one field. Here we re-read package + signature
    race-safe from the configured paths, reload the trust-root from the FIXED baked path, and re-run
    the entire loader (exact canonical encoding, self-hash recompute, Ed25519 verification, plan
    binding, generated/expires/freshness against operation_now, fingerprint derivation). Then the
    freshly reloaded package must match the ApplyContext EXACTLY on every operational field. Any
    difference — a swapped or re-signed package, a changed signature file, a rotated trust-root, an
    expiry that has since passed — fails closed. We NEVER trust ctx.authorization_valid or the
    cached ctx.authorization["signature_valid"]: informational load-time state, not current proof.
    """
    if ctx.authorization_plan_hash is None or ctx.expected_provenance is None:
        return False
    try:
        if ctx._ceremony_package_path is not None:
            # Ceremony context: re-read the EXACT pinned files; env can never redirect this (§6v5).
            status, pkg = _load_authorization(
                ctx.authorization_plan_hash, operation_now,
                package_path=ctx._ceremony_package_path,
                signature_path=ctx._ceremony_signature_path,
                trust_root_path=ctx._ceremony_trust_root_path, use_env_paths=False)
        else:
            status, pkg = _load_authorization(ctx.authorization_plan_hash, operation_now)
    except Exception:  # fail-closed on any loader/IO error
        return False
    if pkg is None or not status.get("signature_valid"):
        return False
    try:
        expected_prov = {
            "commit_sha": ctx.expected_provenance.commit_sha,
            "source_tree_hash": ctx.expected_provenance.source_tree_hash,
            "api_artifact_hash": ctx.expected_provenance.api_artifact_hash,
            "worker_artifact_hash": ctx.expected_provenance.worker_artifact_hash,
            "document_hash": ctx.expected_provenance.document_hash,
        }
        pairs: list[tuple[Any, Any]] = [
            (ctx.authorization_id, pkg.authorization_id),
            (ctx.authorization_package_hash, pkg.authorization_package_hash),
            (ctx.authorization_key_fingerprint, pkg.public_key_fingerprint),
            (ctx.authorization_plan_hash, pkg.plan_hash),
            (ctx.expected_main_sha, pkg.main_commit_sha),
            (ctx.expected_alembic, pkg.alembic_revision),
            (expected_prov, pkg.expected_provenance_fields()),
            (ctx.expected_product_price, pkg.expected_product_price),
            (ctx.expected_active_mappings, pkg.expected_active_mappings),
            (ctx.authorization_generated_at, pkg.generated_at),
            (ctx.authorization_expires_at, pkg.expires_at),
            (ctx.expected_backup_sha256, pkg.backup_expected_sha256),
            (ctx.expected_backup_storage_reference, pkg.backup_storage_reference),
            (ctx.expected_backup_storage_reference_hash, pkg.backup_storage_reference_hash),
            (ctx.operator_reference, pkg.operator_reference),
        ]
    except AttributeError:
        return False
    return all(a == b for a, b in pairs)


def _authorization_gates(m: dict[str, Any], ctx: ApplyContext,
                         now: datetime) -> list[tuple[str, bool]]:
    """Explicit authorization gates (§3v3). The temporal gates use a FRESH ``now`` (from under
    the global lock at apply time), not the timestamp ApplyContext was built with."""
    a = ctx.authorization or {}
    aid_ok = isinstance(ctx.authorization_id, str) and bool(_AUTH_ID_RE.match(ctx.authorization_id))
    ph_ok = _valid_sha256(ctx.authorization_package_hash)
    fp_ok = isinstance(ctx.authorization_key_fingerprint, str) and \
        bool(_FINGERPRINT_RE.match(ctx.authorization_key_fingerprint))
    gen, exp = ctx.authorization_generated_at, ctx.authorization_expires_at
    from cestaplan_api.provenance.authorization import MAX_GENERATION_AGE_SECONDS
    return [
        ("authz_package_present", bool(a.get("package_present"))),
        ("authz_signature_valid", bool(a.get("signature_valid"))),
        ("authz_valid", ctx.authorization_valid is True),
        ("authz_identity_complete",
         aid_ok and ph_ok and fp_ok and gen is not None and exp is not None),
        ("authz_plan_hash_matches",
         ctx.authorization_plan_hash is not None and ctx.authorization_plan_hash == m["plan_hash"]),
        ("authz_id_valid", aid_ok),
        ("authz_package_hash_valid", ph_ok),
        ("authz_key_fingerprint_valid", fp_ok),
        ("authz_generated_before_now", gen is not None and gen <= now),
        ("authz_not_expired", exp is not None and now <= exp),
        ("authz_generation_fresh",
         gen is not None and 0 <= (now - gen).total_seconds() <= MAX_GENERATION_AGE_SECONDS),
        # §2v4: full re-validation of the sealed package under the lock (not a one-field re-read).
        ("authz_revalidated_under_lock", _authorization_revalidated(ctx, now)),
    ]


def _run_all_gates(db: Session, m: dict[str, Any], ctx: ApplyContext, settings: Settings,
                   *, for_apply: bool, operation_now: datetime) -> tuple[list[str], list[str]]:
    # §1v4: ``operation_now`` is the single non-injectable operational clock, captured ONCE by the
    # caller (under the global lock for apply/restore, a fresh read for verify). ctx.now is ignored.
    passed: list[str] = []
    blocking: list[str] = []

    def record(code: str, ok: bool) -> None:
        (passed if ok else blocking).append(code)

    record("plan_hash_intact", _recompute_plan_hash(m) == m["plan_hash"])
    record("plan_not_expired", _plan_age_ok(m, operation_now))
    record("supported_actions_only", _actions_supported(m))
    wgate_ok, _wgate_code = _writer_contract_gate()
    record("writer_contract_v2", wgate_ok)
    prov = _provenance_gates(m, ctx)
    ident = _build_identity_gates(db, ctx)  # §3v2: bind the full build identity
    authz = _authorization_gates(m, ctx, operation_now)  # §3v3/§2v4: plan-bound, re-verified authz
    for code, ok in (*prov, *ident, *authz):
        record(code, ok)
    # §4v3: manifest blockers resolve ONLY under the full conjunction (prov + identity + authz).
    full_prov = all(ok for _, ok in (*prov, *ident, *authz))
    _present, _resolved, unresolved = _resolve_manifest_blockers(
        m, writer_ok=wgate_ok, full_provenance_ok=full_prov)
    record("manifest_blockers_resolved", not unresolved)  # any unresolved blocker fails closed
    for code, ok in _environment_gates(db, settings, ctx):
        record(code, ok)
    for code, ok in _drift_gates(db, m):
        record(code, ok)
    if for_apply:
        record("backup_verified", _backup_gate(db, ctx, operation_now)[0])
    return passed, blocking


def _plan_age_ok(m: dict[str, Any], operation_now: datetime) -> bool:
    # §1v4: plan age is measured against the single operational clock, never ctx.now.
    gen = m.get("generated_at")
    if not gen:
        return False
    try:
        age = (operation_now - _parse_dt(gen)).total_seconds()
    except (ValueError, TypeError):
        return False
    return 0 <= age <= _MAX_PLAN_AGE_SECONDS


def _actions_supported(m: dict[str, Any]) -> bool:
    """Apply v1 (§11): every action is in the v1 set (mark_disputed is BLOCKED), and every side
    effect is an allowlisted create_price_anomaly whose target row exists exactly once."""
    for lane in m["lanes"]:
        row_hashes = [r["integrity"]["full_row_hash"] for r in lane["rows"]]
        for r in lane["rows"]:
            if r["action"] in _V1_BLOCKED_ACTIONS or r["action"] not in _SUPPORTED_ACTIONS:
                return False
        for se in lane.get("proposed_side_effects", []):
            if se.get("type") != "create_price_anomaly":
                return False
            if se.get("anomaly_type") not in _ALLOWED_ANOMALY_TYPES:
                return False
            if se.get("severity") not in _ALLOWED_ANOMALY_SEVERITIES:
                return False
            if row_hashes.count(se.get("target_observation_ref")) != 1:
                return False  # target must exist exactly once in the lane
    return True


# --------------------------------------------------------------------------- #
# Public modes
# --------------------------------------------------------------------------- #
def verify_only(db: Session, m: dict[str, Any], ctx: ApplyContext,
                settings: Settings | None = None) -> dict[str, Any]:
    """Public read-only validation (spec §4A/§10): pins a REPEATABLE READ, READ ONLY snapshot BEFORE
    any query (no bypass), then runs every gate. ZERO writes."""
    _require_postgres(db)
    planner.readonly_preflight(db)
    return _verify_report(db, m, ctx, settings)


# --------------------------------------------------------------------------- #
# Verify-only authorization ceremony (§5-§8v5) — read-only adapter that prepares
# an UNSIGNED request and later verifies a signed package. It NEVER signs, decrypts
# a key, imports a private key, or writes remediation data.
# --------------------------------------------------------------------------- #
CEREMONY_REQUEST_SCHEMA_VERSION = 1
CEREMONY_PACKAGE_SCHEMA_VERSION = 1
_OPERATOR_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@ /-]{0,127}$")


def _ceremony_canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False)


def _deterministic_authorization_id(plan_hash: str) -> str:
    return "remediation-" + hashlib.sha256(
        ("remediation-authorization-id::" + plan_hash).encode()).hexdigest()[:32]


def _enrolled_key_fingerprint() -> str | None:
    """The fingerprint of the SINGLE enrolled trust-root key, or None if not exactly one."""
    from cestaplan_api.provenance import TrustRootError, load_trust_root
    path = _runtime_provenance_paths()[1]
    if not path or not Path(path).is_file():
        return None
    try:
        keys = load_trust_root(path)
    except TrustRootError:
        return None
    if len(keys) != 1:
        return None
    try:
        return hashlib.sha256(bytes.fromhex(keys[0])).hexdigest()[:16]
    except ValueError:
        return None


def _ceremony_preflight_gates(db: Session, m: dict[str, Any], ctx: ApplyContext,
                              settings: Settings, now: datetime) -> list[tuple[str, bool]]:
    """Every read-only gate a request must pass, using the OBSERVED build as the proposed expected
    values (no package needed). The signed package re-establishes these later under the lock."""
    o = ctx.observed_provenance
    app = ctx.app_commit_sha
    live_trust = _observed_trust_root_hash()
    gates: list[tuple[str, bool]] = [
        ("plan_hash_intact", _recompute_plan_hash(m) == m["plan_hash"]),
        ("plan_not_expired", _plan_age_ok(m, now)),
        ("supported_actions_only", _actions_supported(m)),
        ("writer_contract_v2", _writer_contract_gate()[0]),
        ("app_commit_present", _valid_commit(app)),
        ("api_worker_aligned",
         _valid_commit(app) and ctx.deployed_api_sha == app and ctx.deployed_worker_sha == app),
        ("build_doc_commit_matches_app", _eq_commit(o.commit_sha, app)),
        ("build_doc_alembic_matches_live",
         o.alembic_revision is not None and o.alembic_revision == _alembic_revision(db)),
        ("trust_root_matches_document",
         _valid_sha256(live_trust) and live_trust == o.authorization_trust_root_hash),
        ("enrolled_key_present", _enrolled_key_fingerprint() is not None),
        *_document_toolchain_ok().items(),
        *_environment_gates(db, settings, ctx),
        *_drift_gates(db, m),
        ("backup_verified", _backup_gate_observed(db, ctx, now)),
    ]
    return gates


def _backup_gate_observed(db: Session, ctx: ApplyContext, now: datetime) -> bool:
    """The real backup artifact must verify (SHA/perms/pg_restore/version/age). This is the OBSERVED
    backup — the request proposes its SHA + reference; the signer binds them into the package."""
    if ctx.backup is None:
        return False
    ok, _ev = ctx.backup.verify(now, server_version=_server_version(db))
    return ok


def prepare_authorization_request(
        db: Session, m: dict[str, Any], ctx: ApplyContext, *, operator_reference: str,
        settings: Settings | None = None) -> dict[str, Any]:
    """Produce an UNSIGNED authorization request (§7v5): pins the REPEATABLE READ, READ ONLY
    snapshot then delegates to the read-only report. ZERO writes."""
    _require_postgres(db)
    planner.readonly_preflight(db)
    return _prepare_request_report(db, m, ctx, operator_reference=operator_reference,
                                   settings=settings)


def _prepare_request_report(
        db: Session, m: dict[str, Any], ctx: ApplyContext, *, operator_reference: str,
        settings: Settings | None = None) -> dict[str, Any]:
    """The request body over the pinned snapshot. It is NOT a package and NOT an authorization: no
    generated_at/expires_at/package_hash/signature. Every read-only gate must pass first. ZERO
    writes. The local backup path never enters the request or the report."""
    _require_postgres(db)
    _require_manifest_shape(m)
    settings = settings or Settings()
    now = _now_utc()
    o = ctx.observed_provenance
    # Propose the OBSERVED build + live counts as the expected values, so the reused gates evaluate
    # the very values the request will carry.
    ctx.expected_alembic = o.alembic_revision
    ctx.expected_product_price = int(db.scalar(select(func.count()).select_from(ProductPrice)) or 0)
    ctx.expected_active_mappings = int(db.scalar(
        select(func.count()).select_from(ProviderIngredientMapping).where(
            ProviderIngredientMapping.active.is_(True))) or 0)
    if not (isinstance(operator_reference, str) and _OPERATOR_REF_RE.match(operator_reference)):
        raise ApplyManifestInvalid("operator_reference_invalid")
    gates = _ceremony_preflight_gates(db, m, ctx, settings, now)
    blocking = sorted(code for code, ok in gates if not ok)
    fp = _enrolled_key_fingerprint()
    if blocking or fp is None or ctx.backup is None or not _valid_commit(o.commit_sha) \
            or not _valid_sha256(o.authorization_trust_root_hash):
        return {"prepared": False, "request_blockers": blocking, "request": None}
    request = {
        "request_schema_version": CEREMONY_REQUEST_SCHEMA_VERSION,
        "package_schema_version": CEREMONY_PACKAGE_SCHEMA_VERSION,
        "authorization_id": _deterministic_authorization_id(m["plan_hash"]),
        "plan_hash": m["plan_hash"],
        "main_commit_sha": ctx.app_commit_sha,
        "alembic_revision": o.alembic_revision,
        "expected_commit_sha": o.commit_sha,
        "expected_source_hash": o.source_tree_hash,
        "expected_api_artifact_hash": o.api_artifact_hash,
        "expected_worker_artifact_hash": o.worker_artifact_hash,
        "expected_document_hash": o.document_hash,
        "expected_product_price": ctx.expected_product_price,
        "expected_active_mappings": ctx.expected_active_mappings,
        "operator_reference": operator_reference,
        "backup_expected_sha256": ctx.backup.expected_sha256,  # OBSERVED sha, never the path
        "backup_storage_reference": ctx.backup.storage_reference,
        "authorized_key_fingerprint": fp,
        "authorization_trust_root_hash": o.authorization_trust_root_hash,
    }
    request["request_hash"] = hashlib.sha256(_ceremony_canonical(request).encode()).hexdigest()
    return {"prepared": True, "request_blockers": [], "request": request}


def verify_authorization_ceremony(db: Session, m: dict[str, Any],
                                  ctx: ApplyContext) -> dict[str, Any]:
    """Full verify-only over a ceremony context (signed package + real backup). Read-only; ZERO
    writes; sanitized report. apply_ready may reach true when EVERY gate is valid — apply is never
    executed here, and the CLI keeps blocking --apply/--restore/--simulate in cloud."""
    return verify_only(db, m, ctx, settings=Settings())


def _verify_report(db: Session, m: dict[str, Any], ctx: ApplyContext,
                   settings: Settings | None = None) -> dict[str, Any]:
    _require_postgres(db)
    _require_manifest_shape(m)
    settings = settings or Settings()
    # §1v4: verify-only is read-only; it takes a single FRESH operational clock at the start of its
    # snapshot and threads it through every temporal gate. ctx.now is never consulted.
    operation_now = _now_utc()
    passed, blocking = _run_all_gates(
        db, m, ctx, settings, for_apply=True, operation_now=operation_now)
    wgate_ok, _ = _writer_contract_gate()
    full_prov = _full_provenance_ok(db, m, ctx, now=operation_now)
    present, resolved, unresolved = _resolve_manifest_blockers(
        m, writer_ok=wgate_ok, full_provenance_ok=full_prov)
    apply_blockers: list[str] = []
    if not full_prov:
        apply_blockers.append("immutable_build_provenance_missing")
    if not _backup_gate(db, ctx, operation_now)[0]:
        apply_blockers.append("verified_backup_missing")
    apply_blockers.extend(f"unresolved_manifest_blocker:{b}" for b in unresolved)
    report = {
        "apply_tool_version": APPLY_TOOL_VERSION,
        "plan_found": True,
        "plan_hash": m["plan_hash"],
        "manifest_schema_version": m["schema_version"],
        "planner_tool_version": m["tool_version"],
        "writer_contract": writer.writer_contract().get("version"),
        "declared_commit_sha": ctx.app_commit_sha,
        "observed_provenance_document_hash": ctx.observed_provenance.document_hash,
        "lanes": len(m["lanes"]),
        "lanes_excluded": sum(1 for x in m["lanes"] if x.get("excluded")),
        "planned_changes": sum(1 for r in _planned_rows(m) if r["action"] in _ACTION_WRITES),
        "manifest_blockers_present": present,
        "blockers_resolved": resolved,
        "blockers_unresolved": unresolved,
        "gates_passed": sorted(passed),
        "gates_blocking": sorted(blocking),
        "apply_ready": (not blocking) and not apply_blockers,
        "apply_blockers": apply_blockers,
        "provenance": _provenance_report(m, ctx),
        # NOT "authorization": that key name is on the sensitive-key denylist (scan_sensitive).
        "authorization_status": _authorization_report(ctx),
    }
    return report


def simulate(db: Session, m: dict[str, Any], ctx: ApplyContext,
             settings: Settings | None = None) -> dict[str, Any]:
    """Public in-memory validation (spec §4B/§10): requires a clean session, pins the read-only
    snapshot, forbids autoflush, and never writes."""
    _require_postgres(db)
    planner.readonly_preflight(db)
    with db.no_autoflush:
        return _simulate_report(db, m, ctx, settings)


def _simulate_report(db: Session, m: dict[str, Any], ctx: ApplyContext,
                     settings: Settings | None = None) -> dict[str, Any]:
    _require_postgres(db)
    _require_manifest_shape(m)
    settings = settings or Settings()
    # §1v4: simulate is in-memory/read-only — a single fresh operational clock, ctx.now ignored.
    passed, blocking = _run_all_gates(
        db, m, ctx, settings, for_apply=False, operation_now=_now_utc())
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


def _require_clean_session(db: Session) -> None:
    """Reject a session with pending ORM state BEFORE any SQL (spec §10) — typed, never assert."""
    if db.new:
        raise ApplySessionNotClean("session_new")
    if db.dirty:
        raise ApplySessionNotClean("session_dirty")
    if db.deleted:
        raise ApplySessionNotClean("session_deleted")


def _require_virgin_session(db: Session) -> None:
    """The public apply/restore must OWN a freshly-begun transaction (spec §6): no pending ORM
    state, no open transaction (explicit begin / begin_nested / a prior query), no bound connection.
    A reused session must have been rolled back to a truly virgin state first."""
    _require_clean_session(db)
    if db.in_transaction() or db.in_nested_transaction():
        raise ApplySessionNotClean("session_transaction_already_started")


def _validate_retry(db: Session, plan_hash: str, previous_failed_run_id: str | None) -> int | None:
    """Validate an explicit retry link (spec §2). Returns the failed run's internal id to supersede,
    or None. Blocks a missing/mismatched/non-failed/already-superseded/circular previous run."""
    if previous_failed_run_id is None:
        return None
    from cestaplan_api.models import HistoryRemediationRun
    prev = db.execute(select(HistoryRemediationRun).where(
        HistoryRemediationRun.public_id == previous_failed_run_id)).scalar_one_or_none()
    if prev is None:
        raise ApplyManifestInvalid("retry_previous_run_not_found", previous_failed_run_id)
    if prev.plan_hash != plan_hash:
        raise ApplyManifestInvalid("retry_plan_hash_mismatch", previous_failed_run_id)
    if prev.status != "failed":
        raise ApplyManifestInvalid("retry_previous_not_failed", prev.status)
    already = db.execute(select(func.count()).select_from(HistoryRemediationRun).where(
        HistoryRemediationRun.supersedes_run_id == prev.id)).scalar()
    if already:
        raise ApplyManifestInvalid("retry_previous_already_superseded", previous_failed_run_id)
    # Walk the supersedes chain to reject a cycle.
    seen, cur = {prev.id}, prev
    while cur.supersedes_run_id is not None:
        if cur.supersedes_run_id in seen:
            raise ApplyManifestInvalid("retry_circular_chain", previous_failed_run_id)
        seen.add(cur.supersedes_run_id)
        cur = db.get(HistoryRemediationRun, cur.supersedes_run_id)
        if cur is None:
            break
    return prev.id


def _backup_run_fields(ctx: ApplyContext, bev: dict[str, Any]) -> dict[str, Any]:
    return {
        "backup_sha256": bev.get("observed_sha256"),
        "backup_size_bytes": bev.get("size_bytes"),
        "backup_postgres_version": bev.get("expected_postgres_version"),
        "backup_pg_restore_version": bev.get("observed_pg_restore_version"),
        "backup_database_version": bev.get("observed_database_version"),
        "backup_dump_database_version": bev.get("dump_database_version"),
        "backup_restore_list_verified": bev.get("pg_restore_list_verified"),
        "backup_permissions_verified": bev.get("permissions_not_public"),
        "backup_created_at": ctx.backup.created_at if ctx.backup is not None else None,
        # Only the SANITIZED reference (+ its hash) is ever persisted — never the raw value or the
        # local dump path (§4v4).
        "backup_storage_reference": bev.get("storage_reference_sanitized"),
        "backup_storage_reference_hash": bev.get("storage_reference_hash"),
        "backup_evidence_hash": planner._value_hash(bev),
    }


def _live_occ_fk(db: Session, obs_ids: list[int]) -> tuple[dict, dict, list]:
    """Recompute the sanitized occurrence/supported-FK evidence for a set of observations. Values
    are JSON-safe (lists, not tuples) so they compare cleanly against the JSONB-roundtripped copy.
    Returns (occurrence_hashes, supported_fk_hashes, discovered_fks)."""
    occ: dict[str, list[str]] = {}
    for oid in obs_ids:
        occ[str(oid)] = sorted(
            planner._occ_manifest(o)["occurrence_hash"]
            for o in db.execute(select(planner.PriceObservationOccurrence).where(
                planner.PriceObservationOccurrence.price_observation_id == oid)).scalars())
    discovered = planner.discover_incoming_fks(db)
    fk: dict[str, list[list]] = {str(oid): [] for oid in obs_ids}
    for fkd in discovered:
        if fkd.get("classification") != "domain_supported":
            continue
        handler = planner._FK_HANDLERS.get(planner._fk_key(fkd))
        if not handler or not handler["emit"] or handler["model"] is None:
            continue
        model = handler["model"]
        col = getattr(model, fkd["referencing_column"])
        for row in db.execute(select(model).where(col.in_(obs_ids))).scalars():
            fk[str(getattr(row, fkd["referencing_column"]))].append(
                json.loads(json.dumps(list(_supported_fk_identity(
                    planner._fk_manifest(fkd, model, row))))))
    return occ, {k: sorted(v) for k, v in fk.items()}, discovered


def _capture_post_apply_evidence(db: Session, m: dict[str, Any], run: Any) -> None:
    """Store sanitized post-apply evidence so a restore can re-verify with the same controls without
    the (deleted) manifest (spec §4)."""
    obs_ids = [r["id"] for r in _planned_rows(m)]
    occ, fk, discovered = _live_occ_fk(db, obs_ids)
    run.post_apply_occurrence_hashes = occ
    run.post_apply_supported_fk_hashes = fk
    run.discovered_fk_fingerprint = planner._value_hash(sorted(planner._fk_ident(f) for f in
                                                               discovered))
    run.expected_unknown_fk_count = 0


def _lane_lock_key(lane_fp: str) -> int:
    return ident.signed_bigint(hashlib.sha256(lane_fp.encode()).hexdigest())


def _det_action_id(plan_hash: str, lane_fp: str, full_row_hash: str, action: str,
                   side_effect_ref: str) -> str:
    return hashlib.sha256(
        "\x1f".join((plan_hash, lane_fp, full_row_hash, action, side_effect_ref)).encode()
    ).hexdigest()


def _anomaly_hash(an: PriceAnomaly) -> str:
    return planner._value_hash({"price_observation_id": an.price_observation_id,
                                "anomaly_type": an.anomaly_type, "severity": an.severity,
                                "status": an.status})


def _record_failed_run(m: dict[str, Any], ctx: ApplyContext, error_code: str,
                       supersedes: int | None = None) -> str | None:
    """Durable failure audit in a SEPARATE transaction (§5) so a failed run survives the data
    rollback and a retry can link to it via supersedes_run_id. Sanitized code only, no raw msg.
    Returns the failed run's public_id."""
    from cestaplan_api.models import HistoryRemediationRun
    s = SessionLocal()
    try:
        o, e = ctx.observed_provenance, ctx.expected_provenance
        # §5v4: NEVER fabricate empty-string evidence. main_commit_sha is the REAL observed commit
        # (APP_COMMIT_SHA, else the baked document's commit); alembic_revision is read LIVE from the
        # database during THIS audit transaction. When neither yields a real value -> NULL (the
        # columns are nullable), never "".
        observed_main = ctx.app_commit_sha or (o.commit_sha if o is not None else None) or None
        audit_alembic = _alembic_revision(s) or None
        # §1v4: the failure audit uses the single non-injectable clock (_now_utc), never ctx.now.
        audit_now = _now_utc()
        run = HistoryRemediationRun(
            plan_hash=m["plan_hash"], manifest_schema_version=m.get("schema_version", 0),
            planner_tool_version=m.get("tool_version", ""),
            planner_source_hash=m.get("planner_source_hash", ""),
            writer_contract_version=REQUIRED_WRITER_CONTRACT,
            main_commit_sha=observed_main,
            alembic_revision=audit_alembic,
            execution_mode="apply", status="failed", error_code=error_code,
            started_at=audit_now, completed_at=audit_now,
            operator_reference=ctx.operator_reference,  # already sanitized upstream
            supersedes_run_id=supersedes,
            # Preserve identity ESTABLISHED before the failure; absent stays NULL — never
            # invent a value (§6v3).
            observed_commit_sha=ctx.app_commit_sha, expected_commit_sha=e.commit_sha,
            observed_source_hash=o.source_tree_hash, expected_source_hash=e.source_tree_hash,
            observed_api_artifact_hash=o.api_artifact_hash,
            expected_api_artifact_hash=e.api_artifact_hash,
            observed_worker_artifact_hash=o.worker_artifact_hash,
            expected_worker_artifact_hash=e.worker_artifact_hash,
            observed_provenance_document_hash=o.document_hash,
            expected_provenance_document_hash=e.document_hash,
            authorization_id=ctx.authorization_id,
            authorization_package_hash=ctx.authorization_package_hash,
            authorization_key_fingerprint=ctx.authorization_key_fingerprint,
            authorization_generated_at=ctx.authorization_generated_at,
            authorization_expires_at=ctx.authorization_expires_at,
            expected_backup_sha256=ctx.expected_backup_sha256,
            expected_backup_storage_reference_hash=ctx.expected_backup_storage_reference_hash)
        s.add(run)
        s.commit()
        return str(run.public_id)
    finally:
        s.close()


def execute_apply(m: dict[str, Any], ctx: ApplyContext, *,
                  session_factory: Callable[[], Session] = SessionLocal, authorized: bool = False,
                  confirmations: tuple[str, ...] = (), previous_failed_run_id: str | None = None,
                  settings: Settings | None = None, lock_timeout_ms: int = 5000) -> dict[str, Any]:
    """OPERATIONAL apply entrypoint (spec §1v4). This function OWNS the Session and the single
    transaction end-to-end: it creates a fresh Session, proves it is virgin, runs locks / gates /
    writes / verification, COMMITS before returning a success status, and closes the Session. On any
    failure (including a commit that fails) it rolls back, records a durable failed run in an
    INDEPENDENT transaction and re-raises the original. A returned status of ``applied`` therefore
    means the run, changes, plan-consumption row and temporal writes are durably visible to other
    connections."""
    settings = settings or Settings()
    db = session_factory()
    try:
        _require_virgin_session(db)
        result = _apply_guarded(db, m, ctx, authorized=authorized, confirmations=confirmations,
                                previous_failed_run_id=previous_failed_run_id, settings=settings,
                                lock_timeout_ms=lock_timeout_ms)
        try:
            db.commit()  # success is only returned AFTER a durable commit
        except Exception as exc:
            db.rollback()
            exc.failed_run_id = _record_failed_run(  # type: ignore[attr-defined]
                m, ctx, "apply_commit_failed")
            raise
        return result
    finally:
        db.close()


def apply(db: Session, m: dict[str, Any], ctx: ApplyContext, *, authorized: bool = False,
          confirmations: tuple[str, ...] = (), previous_failed_run_id: str | None = None,
          settings: Settings | None = None, lock_timeout_ms: int = 5000) -> dict[str, Any]:
    """NON-operational helper for tests only (spec §1v4): requires a VIRGIN session but does NOT
    commit — the caller owns durability. Operational callers must use :func:`execute_apply`."""
    _require_virgin_session(db)
    return _apply_guarded(db, m, ctx, authorized=authorized, confirmations=confirmations,
                          previous_failed_run_id=previous_failed_run_id, settings=settings,
                          lock_timeout_ms=lock_timeout_ms)


def _apply_guarded(db: Session, m: dict[str, Any], ctx: ApplyContext, *, authorized: bool = False,
                   confirmations: tuple[str, ...] = (), previous_failed_run_id: str | None = None,
                   settings: Settings | None = None,
                   lock_timeout_ms: int = 5000) -> dict[str, Any]:
    """Auth + gates + atomic apply with durable failure auditing (§3/§5). ANY unexpected exception
    (not just ApplyError, never KeyboardInterrupt/SystemExit) rolls back and records a durable
    run with a SANITIZED code, then re-raises the original."""
    _require_authorization(authorized, confirmations)
    _require_postgres(db)
    _require_clean_session(db)
    _require_manifest_shape(m)
    settings = settings or Settings()
    supersedes = _validate_retry(db, m["plan_hash"], previous_failed_run_id)
    try:
        return _apply_locked(db, m, ctx, settings, lock_timeout_ms, supersedes)
    except ApplyAlreadyApplied:
        raise
    except ApplyError as exc:
        db.rollback()
        exc.failed_run_id = _record_failed_run(m, ctx, exc.code, supersedes)
        raise
    except Exception as exc:
        db.rollback()
        fid = _record_failed_run(m, ctx, "unexpected_apply_error", supersedes)
        exc.failed_run_id = fid  # type: ignore[attr-defined]
        raise


def _apply_locked(db: Session, m: dict[str, Any], ctx: ApplyContext, settings: Settings,
                  lock_timeout_ms: int, supersedes: int | None) -> dict[str, Any]:
    from cestaplan_api.models import HistoryRemediationPlanConsumption, HistoryRemediationRun
    # 1) global remediation lock; then the irreversible-consumption gate (§1).
    _acquire(db, _GLOBAL_LOCK_KEY, timeout_ms=lock_timeout_ms)
    consumed = db.execute(select(HistoryRemediationPlanConsumption).where(
        HistoryRemediationPlanConsumption.plan_hash == m["plan_hash"])).scalar_one_or_none()
    if consumed is not None:
        if _completed_run(db, m["plan_hash"]) is not None:
            return {"status": "already_applied", "plan_hash": m["plan_hash"]}
        # Consumed once but not currently applied -> it was restored; a NEW plan is required.
        return {"status": "plan_requires_regeneration", "plan_hash": m["plan_hash"]}
    # 2) deterministic lane locks; 3) capture the SINGLE operational clock ONCE, now that the global
    #    lock is held, and thread it through every temporal gate + written timestamp. The
    #    authorization temporal gates cannot be satisfied by a stale ApplyContext.now (§1v4/§3v3).
    for lane in sorted(m["lanes"], key=lambda x: x["lane_fingerprint"]):
        if not lane.get("excluded"):
            _acquire(db, _lane_lock_key(lane["lane_fingerprint"]), timeout_ms=lock_timeout_ms)
    operation_now = _now_utc()
    _passed, blocking = _run_all_gates(
        db, m, ctx, settings, for_apply=True, operation_now=operation_now)
    if blocking:
        raise ApplyEnvironmentUnsafe("gates_blocking", ",".join(sorted(blocking)))

    run_ts = operation_now  # run_ts / started_at / completed_at all bind to the one clock
    before = _count_snapshot(db)
    o, e = ctx.observed_provenance, ctx.expected_provenance
    bev = _backup_gate(db, ctx, operation_now)[1]
    run = HistoryRemediationRun(
        plan_hash=m["plan_hash"], manifest_schema_version=m["schema_version"],
        planner_tool_version=m["tool_version"], planner_source_hash=m["planner_source_hash"],
        writer_contract_version=REQUIRED_WRITER_CONTRACT,
        main_commit_sha=ctx.expected_main_sha or m["commit_provenance"].get("base_main_sha"),
        alembic_revision=_alembic_revision(db), execution_mode="apply", status="applied",
        started_at=run_ts, operator_reference=ctx.operator_reference, before_counts=before,
        supersedes_run_id=supersedes,
        observed_commit_sha=ctx.app_commit_sha, expected_commit_sha=e.commit_sha,
        observed_source_hash=o.source_tree_hash, expected_source_hash=e.source_tree_hash,
        observed_api_artifact_hash=o.api_artifact_hash,
        expected_api_artifact_hash=e.api_artifact_hash,
        observed_worker_artifact_hash=o.worker_artifact_hash,
        expected_worker_artifact_hash=e.worker_artifact_hash,
        expected_provenance_document_hash=e.document_hash,
        observed_provenance_document_hash=o.document_hash,
        authorization_id=ctx.authorization_id,
        authorization_package_hash=ctx.authorization_package_hash,
        authorization_key_fingerprint=ctx.authorization_key_fingerprint,
        authorization_generated_at=ctx.authorization_generated_at,
        authorization_expires_at=ctx.authorization_expires_at,
        expected_backup_sha256=ctx.expected_backup_sha256,
        expected_backup_storage_reference_hash=ctx.expected_backup_storage_reference_hash,
        **_backup_run_fields(ctx, bev))
    db.add(run)
    try:
        db.flush()  # unique(plan_hash where status=applied) -> concurrent duplicate fails here
    except IntegrityError as exc:
        raise ApplyAlreadyApplied("plan_hash_already_applied", str(exc)[:80]) from exc
    run_ref = str(run.public_id)

    changes: list[HistoryRemediationChange] = []
    with _WriteGuard(db):
        live = {o2.id: o2 for o2 in db.execute(select(PriceObservation).where(
            PriceObservation.id.in_([r["id"] for r in _planned_rows(m)])).with_for_update()
        ).scalars()}
        for lane in m["lanes"]:
            if lane.get("excluded"):
                continue
            anomaly_by_target = _apply_side_effects(db, lane)
            for r in lane["rows"]:
                changes.append(_apply_row(db, run, m, lane, r, live[r["id"]], run_ts,
                                          anomaly_by_target))
        db.flush()
        # 5) post-flush verification: fill actual_after, require it EXACTLY matches expected, and
        # SEAL each change's full evidence (§1v5).
        for ch in changes:
            actual = _temporal_of(live[ch.price_observation_id])
            ch.actual_after_state = _json(actual)
            ch.actual_after_hash = _thash(actual)
            ch.status = "applied"
            ch.error_code = None
            if ch.action_type in _ACTION_WRITES and ch.actual_after_hash != ch.expected_bound_hash:
                raise ApplyPlanDrift("post_flush_mismatch", str(ch.price_observation_id))
            ch.apply_evidence_hash = _apply_evidence_hash(ch, m["plan_hash"])
    after = _count_snapshot(db)
    run.after_counts = after
    run.completed_at = operation_now  # §1v4: the one operation clock, not ctx.now
    _capture_post_apply_evidence(db, m, run)  # §4: evidence a restore can re-verify against
    # §2v5: seal the whole run AFTER its post-apply evidence is set; the consumption row copies it.
    run.execution_hash = _run_execution_hash(run, changes)
    _assert_counts_preserved(before, after)
    # §1: durably mark this plan_hash consumed; NEVER deleted, not even by a restore.
    db.add(HistoryRemediationPlanConsumption(
        plan_hash=m["plan_hash"], first_run_id=run.id, applied_at=run_ts,
        execution_hash=run.execution_hash))
    db.flush()
    return {"status": "applied", "plan_hash": m["plan_hash"], "run_public_id": run_ref,
            "changes": len(changes), "before_counts": before, "after_counts": after}


def _apply_side_effects(db: Session, lane: dict[str, Any]) -> dict[str, PriceAnomaly]:
    """Create ONLY the manifest-proposed anomalies (already allowlisted by _actions_supported),
    refusing to duplicate an equivalent open anomaly (spec §11)."""
    out: dict[str, PriceAnomaly] = {}
    for se in lane.get("proposed_side_effects", []):
        if se.get("type") != "create_price_anomaly":
            raise ApplyUnsupportedAction("unsupported_side_effect", str(se.get("type")))
        obs_id = _obs_id_for_hash(lane, se["target_observation_ref"])
        existing = db.execute(select(func.count()).select_from(PriceAnomaly).where(
            PriceAnomaly.price_observation_id == obs_id,
            PriceAnomaly.anomaly_type == se["anomaly_type"],
            PriceAnomaly.status == "open")).scalar()
        if existing:
            raise ApplyUnsupportedAction("equivalent_anomaly_exists", str(obs_id))
        an = PriceAnomaly(price_observation_id=obs_id, anomaly_type=se["anomaly_type"],
                          severity=se["severity"], status="open")
        db.add(an)
        db.flush()
        out[se["target_observation_ref"]] = an
    return out


def _obs_id_for_hash(lane: dict[str, Any], full_row_hash: str) -> int | None:
    for r in lane["rows"]:
        if r["integrity"]["full_row_hash"] == full_row_hash:
            return r["id"]
    return None


def _sealed_side_effect_ref(lane: dict[str, Any], full_row_hash: str) -> str:
    """A stable side-effect reference derived ONLY from the sealed manifest (spec §9) — never a
    database id generated during execution, so deterministic_action_id is computable before any
    INSERT and is identical across runs of the same plan."""
    for se in lane.get("proposed_side_effects", []):
        if se.get("target_observation_ref") == full_row_hash:
            return hashlib.sha256("\x1f".join((
                str(se.get("type")), str(se.get("target_observation_ref")),
                str(se.get("anomaly_type")), str(se.get("severity")))).encode()).hexdigest()
    return ""


def _apply_row(db: Session, run: HistoryRemediationRun, m: dict[str, Any], lane: dict[str, Any],
               r: dict[str, Any], live: PriceObservation, run_ts: datetime,
               anomaly_by_target: dict[str, PriceAnomaly]) -> HistoryRemediationChange:
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
        remediation_run_id=run.id,
        deterministic_action_id=_det_action_id(
            m["plan_hash"], lane["lane_fingerprint"], r["integrity"]["full_row_hash"], action,
            _sealed_side_effect_ref(lane, r["integrity"]["full_row_hash"])),
        lane_fingerprint=lane["lane_fingerprint"], price_observation_id=r["id"], action_type=action,
        original_temporal_state=_json(original), expected_bound_state=_json(bound),
        original_hash=r["integrity"]["full_row_hash"], expected_bound_hash=_thash(bound),
        created_anomaly_original_id=anomaly.id if anomaly is not None else None,
        created_anomaly_live_id=anomaly.id if anomaly is not None else None,
        created_anomaly_hash=_anomaly_hash(anomaly) if anomaly is not None else None,
        apply_evidence_hash="pending",  # sealed after post-flush once actual_after is known
        status="planned")
    db.add(ch)
    return ch


_RESTORE_ENV_GATES = ("production_disabled", "per_chain_flags_false", "price_provider_kill_switch",
                      "crawl_run_not_running", "crawl_job_not_active")


def execute_restore(run_public_id: str, ctx: ApplyContext, *,
                    session_factory: Callable[[], Session] = SessionLocal, authorized: bool = False,
                    confirmations: tuple[str, ...] = (), settings: Settings | None = None,
                    lock_timeout_ms: int = 5000) -> dict[str, Any]:
    """OPERATIONAL restore entrypoint (spec §1v4). OWNS the Session and single transaction: creates
    a fresh virgin Session, runs the full gates + all-rows revalidation + per-anomaly verification,
    COMMITS before returning ``restored``, and closes the Session. On drift it rolls back, persists
    ``manual_review_required`` in an INDEPENDENT transaction and re-raises; a commit that fails is
    treated the same way. ``restored`` therefore means the rolled_back run, restore_status, restored
    rows, historical anomaly references and surviving plan-consumption are all durably visible."""
    settings = settings or Settings()
    db = session_factory()
    try:
        _require_virgin_session(db)
        result = _restore_guarded(db, run_public_id, ctx, authorized=authorized,
                                  confirmations=confirmations, settings=settings,
                                  lock_timeout_ms=lock_timeout_ms)
        try:
            db.commit()
        except Exception:
            db.rollback()
            _mark_manual_review(run_public_id, "restore_commit_failed")
            raise
        return result
    finally:
        db.close()


def restore(db: Session, run_public_id: str, ctx: ApplyContext, *, authorized: bool = False,
            confirmations: tuple[str, ...] = (), settings: Settings | None = None,
            lock_timeout_ms: int = 5000) -> dict[str, Any]:
    """NON-operational helper for tests only (spec §1v4): requires a VIRGIN session but does NOT
    commit. Operational callers must use :func:`execute_restore`."""
    _require_virgin_session(db)
    return _restore_guarded(db, run_public_id, ctx, authorized=authorized,
                            confirmations=confirmations, settings=settings,
                            lock_timeout_ms=lock_timeout_ms)


def _restore_guarded(db: Session, run_public_id: str, ctx: ApplyContext, *,
                     authorized: bool = False, confirmations: tuple[str, ...] = (),
                     settings: Settings | None = None,
                     lock_timeout_ms: int = 5000) -> dict[str, Any]:
    """Exactly restore one apply run (spec §4D/§6): global + lane locks, the SAME provenance /
    contract / env gates as apply, revalidation against the stored post-apply evidence, SELECT FOR
    UPDATE, per-anomaly verification before delete, post-flush verify, atomic. On drift, roll back
    manual_review_required in a SEPARATE audit transaction so the state is never lost."""
    from cestaplan_api.models import HistoryRemediationChange, HistoryRemediationRun
    _require_authorization(authorized, confirmations, restore=True)
    _require_postgres(db)
    _require_clean_session(db)
    settings = settings or Settings()
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
    for lane_fp in sorted({c.lane_fingerprint for c in changes}):
        _acquire(db, _lane_lock_key(lane_fp), timeout_ms=lock_timeout_ms)
    # Same provenance/contract/env controls as apply (§4/§6), incl. a valid + current + plan-bound
    # authorization — restore is only allowed within the ORIGINAL package's validity window (§5v3).
    # §1v4: capture the single operational clock ONCE, now that the global + lane locks are held.
    operation_now = _now_utc()
    rm = {"plan_hash": run.plan_hash, "commit_provenance": {}}
    env = dict(_environment_gates(db, settings, ctx))
    if not all(env.get(g) for g in _RESTORE_ENV_GATES) or not _writer_contract_gate()[0] \
            or not _full_provenance_ok(db, rm, ctx, now=operation_now):
        raise ApplyEnvironmentUnsafe("restore_gates_blocking", "")
    # ... AND the current context must bind EXACTLY to the run's stored evidence + authorization
    # identity (§3v4/§5v3): the same build AND the same signed package that applied it. A different
    # package — even signed by the same key with the same provenance — is rejected.
    _restore_provenance_bound_to_run(db, run, ctx)
    try:
        _restore_evidence_gates(db, run, changes)  # occurrences / FK / unknown-FK unchanged (§4)
        return _restore_locked(db, run, changes, operation_now)
    except (ApplyRestoreDrift, ApplyForbiddenWrite) as exc:
        db.rollback()
        _mark_manual_review(run_public_id, exc.code)
        raise


def _restore_provenance_bound_to_run(db: Session, run: HistoryRemediationRun,
                                     ctx: ApplyContext) -> None:
    """Restore must run under the SAME build + sealed package that applied the run (spec §3v4).

    Every provenance field is compared EXACTLY against the evidence persisted on the run — the run
    is the source of the expected values, never a freely-substituted runtime package. Any mismatch
    (later build, different commit/api/worker/alembic/document) fails closed.
    """
    o, e = ctx.observed_provenance, ctx.expected_provenance
    checks: list[tuple[str, Any, Any]] = [
        ("main_commit_sha", run.main_commit_sha, ctx.expected_main_sha),
        ("alembic_revision_expected", run.alembic_revision, ctx.expected_alembic),
        ("alembic_revision_live", run.alembic_revision, _alembic_revision(db)),
        ("observed_commit_sha", run.observed_commit_sha, ctx.app_commit_sha),
        ("expected_commit_sha", run.expected_commit_sha, e.commit_sha),
        ("observed_source_hash", run.observed_source_hash, o.source_tree_hash),
        ("expected_source_hash", run.expected_source_hash, e.source_tree_hash),
        ("observed_api_artifact_hash", run.observed_api_artifact_hash, o.api_artifact_hash),
        ("expected_api_artifact_hash", run.expected_api_artifact_hash, e.api_artifact_hash),
        ("observed_worker_artifact_hash",
         run.observed_worker_artifact_hash, o.worker_artifact_hash),
        ("expected_worker_artifact_hash",
         run.expected_worker_artifact_hash, e.worker_artifact_hash),
        ("observed_provenance_document_hash",
         run.observed_provenance_document_hash, o.document_hash),
        ("expected_provenance_document_hash",
         run.expected_provenance_document_hash, e.document_hash),
        # §5v3: bind the EXACT signed package that applied the run (id, self-hash, key fingerprint,
        # validity window, expected backup) + the plan_hash. A different package fails closed.
        ("authorization_id", run.authorization_id, ctx.authorization_id),
        ("authorization_package_hash", run.authorization_package_hash,
         ctx.authorization_package_hash),
        ("authorization_key_fingerprint", run.authorization_key_fingerprint,
         ctx.authorization_key_fingerprint),
        ("authorization_generated_at", _iso_utc(run.authorization_generated_at),
         _iso_utc(ctx.authorization_generated_at)),
        ("authorization_expires_at", _iso_utc(run.authorization_expires_at),
         _iso_utc(ctx.authorization_expires_at)),
        ("expected_backup_sha256", run.expected_backup_sha256, ctx.expected_backup_sha256),
        ("expected_backup_storage_reference_hash", run.expected_backup_storage_reference_hash,
         ctx.expected_backup_storage_reference_hash),
        ("authorization_plan_hash", run.plan_hash, ctx.authorization_plan_hash),
    ]
    for name, run_value, ctx_value in checks:
        if run_value != ctx_value:
            raise ApplyProvenanceMismatch("restore_provenance_mismatch", name)


def _restore_evidence_gates(db: Session, run: HistoryRemediationRun,
                            changes: list[HistoryRemediationChange]) -> None:
    """Revalidate against the sanitized POST-APPLY evidence stored on the run — never the deleted
    manifest (spec §4). Any drift fails closed."""
    obs_ids = [c.price_observation_id for c in changes]
    live_occ, live_fk, discovered = _live_occ_fk(db, obs_ids)
    stored_occ = json.loads(json.dumps(run.post_apply_occurrence_hashes or {}))
    stored_fk = json.loads(json.dumps(run.post_apply_supported_fk_hashes or {}))
    if live_occ != stored_occ:
        raise ApplyRestoreDrift("occurrences_changed_after_apply")
    if planner._unknown_fk_refs(db, discovered, obs_ids) or \
            (run.expected_unknown_fk_count or 0) != 0:
        raise ApplyRestoreDrift("unknown_fk_after_apply")
    if live_fk != stored_fk:
        raise ApplyRestoreDrift("supported_fk_changed_after_apply")
    if planner._value_hash(sorted(planner._fk_ident(f) for f in discovered)) != \
            run.discovered_fk_fingerprint:
        raise ApplyRestoreDrift("fk_schema_changed_after_apply")


def _verify_anomaly_before_delete(db: Session, ch: HistoryRemediationChange) -> PriceAnomaly:
    """The anomaly to delete must EXACTLY match what this run created (spec §5) or fail closed."""
    an = db.get(PriceAnomaly, ch.created_anomaly_live_id)
    if an is None or an.id != ch.created_anomaly_live_id:
        raise ApplyRestoreDrift("anomaly_missing", str(ch.created_anomaly_live_id))
    if _anomaly_hash(an) != ch.created_anomaly_hash or \
            an.price_observation_id != ch.price_observation_id:
        raise ApplyRestoreDrift("anomaly_changed", str(an.id))
    return an


def _change_side_effect_ref(ch: HistoryRemediationChange, an: PriceAnomaly | None) -> str:
    """Reconstruct the sealed side-effect reference for a change from its VERIFIED anomaly (or empty
    when the change carried no side effect), so deterministic_action_id can be recomputed at restore
    without the deleted manifest (spec §2v4/§9). Mirrors :func:`_sealed_side_effect_ref`."""
    if an is None:
        return ""
    payload = "\x1f".join(
        ("create_price_anomaly", ch.original_hash, an.anomaly_type, an.severity))
    return hashlib.sha256(payload.encode()).hexdigest()


def _validate_all_changes(db: Session, run: HistoryRemediationRun,
                          changes: list[HistoryRemediationChange],
                          rows: dict[int, PriceObservation],
                          anomalies: dict[Any, PriceAnomaly]) -> None:
    """Before restoring ANY row, every change (keep / excluded_no_action / logical_rollback /
    reconstruct_interval alike — not only writes) must still match its recorded post-apply state and
    the audit must be internally consistent (spec §2v4). Any failure fails closed, zero writes."""
    # Pass 1 — structural uniqueness across ALL changes (independent of live-row state), so a true
    # duplicate is always reported as such regardless of row iteration order.
    seen_obs_action: set[tuple[int, str]] = set()
    seen_det: set[str] = set()
    for ch in changes:
        oa = (ch.price_observation_id, ch.action_type)
        if oa in seen_obs_action:
            raise ApplyRestoreDrift("duplicate_change_for_observation",
                                    str(ch.price_observation_id))
        seen_obs_action.add(oa)
        if ch.deterministic_action_id in seen_det:
            raise ApplyRestoreDrift("duplicate_deterministic_action_id", ch.deterministic_action_id)
        seen_det.add(ch.deterministic_action_id)
    # Pass 2 — per-row post-apply state + audit consistency (ALL actions, not only writes).
    for ch in changes:
        cnt = db.scalar(select(func.count()).select_from(PriceObservation).where(
            PriceObservation.id == ch.price_observation_id))
        if cnt != 1 or ch.price_observation_id not in rows:
            raise ApplyRestoreDrift("observation_missing_or_duplicated",
                                    str(ch.price_observation_id))
        if ch.actual_after_hash is None or ch.actual_after_state is None:
            raise ApplyRestoreDrift("actual_after_missing", str(ch.price_observation_id))
        live = _temporal_of(rows[ch.price_observation_id])
        if _thash(live) != ch.actual_after_hash:
            raise ApplyRestoreDrift("row_changed_after_apply", str(ch.price_observation_id))
        if _json(live) != ch.actual_after_state:
            raise ApplyRestoreDrift("actual_after_state_mismatch", str(ch.price_observation_id))
        ref = _change_side_effect_ref(ch, anomalies.get(ch.created_anomaly_live_id))
        expected = _det_action_id(run.plan_hash, ch.lane_fingerprint, ch.original_hash,
                                  ch.action_type, ref)
        if expected != ch.deterministic_action_id:
            raise ApplyRestoreDrift("deterministic_action_id_invalid", ch.deterministic_action_id)


def _verify_sealed_evidence(db: Session, run: HistoryRemediationRun,
                            changes: list[HistoryRemediationChange]) -> None:
    """Before ANY restore write, prove the sealed evidence is intact (spec §2v5): recompute every
    change's apply_evidence_hash and the run's execution_hash, and confirm the immutable
    plan-consumption row carries the SAME execution_hash and points at this run. Fails closed."""
    from cestaplan_api.models import HistoryRemediationPlanConsumption
    for ch in changes:
        if _apply_evidence_hash(ch, run.plan_hash) != ch.apply_evidence_hash:
            raise ApplyRestoreDrift("apply_evidence_hash_mismatch", str(ch.price_observation_id))
    if _run_execution_hash(run, changes) != run.execution_hash:
        raise ApplyRestoreDrift("run_execution_hash_mismatch")
    cons = db.execute(select(HistoryRemediationPlanConsumption).where(
        HistoryRemediationPlanConsumption.plan_hash == run.plan_hash)).scalar_one_or_none()
    if cons is None or cons.execution_hash != run.execution_hash:
        raise ApplyRestoreDrift("consumption_execution_hash_mismatch")
    if cons.first_run_id != run.id:
        raise ApplyRestoreDrift("consumption_first_run_mismatch")


def _restore_locked(db: Session, run: HistoryRemediationRun,
                    changes: list[HistoryRemediationChange],
                    operation_now: datetime) -> dict[str, Any]:
    allowed_ids = frozenset(c.created_anomaly_live_id for c in changes
                            if c.created_anomaly_live_id is not None)
    with _WriteGuard(db, allow_anomaly_delete=True, allowed_anomaly_ids=allowed_ids):
        rows = {o.id: o for o in db.execute(select(PriceObservation).where(
            PriceObservation.id.in_([c.price_observation_id for c in changes])
        ).with_for_update()).scalars()}
        # The sealed evidence must be intact before anything else (§2v5).
        _verify_sealed_evidence(db, run, changes)
        # Verify each anomaly BEFORE any write; a mismatch aborts the whole restore (§5).
        anomalies = {ch.created_anomaly_live_id: _verify_anomaly_before_delete(db, ch)
                     for ch in changes if ch.created_anomaly_live_id is not None}
        # ALL rows must match their recorded post-apply state before any restoration begins (§2v4).
        _validate_all_changes(db, run, changes, rows, anomalies)
        for ch in changes:
            row = rows[ch.price_observation_id]
            for k in WHITELIST_FIELDS:
                setattr(row, k, _parse_dt(ch.original_temporal_state.get(k))
                        if k in ("valid_from", "valid_until", "rolled_back_at")
                        else ch.original_temporal_state.get(k))
            ch.restore_state = _json(_temporal_of(row))
            ch.status = "restored"
        for ch in changes:
            if ch.created_anomaly_live_id is None:
                continue
            an = anomalies[ch.created_anomaly_live_id]
            ch.created_anomaly_live_id = None  # null the live FK; original id + hash are preserved
            db.flush()
            db.delete(an)  # ORM single-id delete of the exact verified object
            ch.created_anomaly_deleted_at = operation_now  # §1v4: the one operation clock
        db.flush()
        for ch in changes:
            if _thash(_temporal_of(rows[ch.price_observation_id])) != _thash(
                    ch.original_temporal_state):
                raise ApplyRestoreDrift("post_restore_mismatch", str(ch.price_observation_id))
    run.restore_status = "restored"
    run.status = "rolled_back"
    db.flush()
    return {"status": "restored", "run_public_id": str(run.public_id),
            "restored_rows": len(changes)}


def _mark_manual_review(run_public_id: str, code: str) -> None:
    """Persist manual_review_required in a SEPARATE transaction so the restore-drift rollback
    does not erase it (spec §6)."""
    from cestaplan_api.models import HistoryRemediationRun
    s = SessionLocal()
    try:
        run = s.execute(select(HistoryRemediationRun).where(
            HistoryRemediationRun.public_id == run_public_id)).scalar_one_or_none()
        if run is not None:
            run.restore_status = "manual_review_required"
            run.error_code = code
            s.commit()
    finally:
        s.close()


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


def _iso_utc(dt: datetime | None) -> str | None:
    """A stable UTC isoformat for sealing (survives the DB timestamptz round-trip)."""
    return dt.astimezone(UTC).isoformat() if dt is not None else None


def _canonical_sha256(payload: Any) -> str:
    """SHA-256 over a canonical JSON serialization: keys sorted, stable nulls, no whitespace, stable
    unicode. Stored temporal states are already UTC-normalized dicts (spec §1v5)."""
    return hashlib.sha256(json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        default=str).encode()).hexdigest()


def _apply_evidence_hash(ch: HistoryRemediationChange, plan_hash: str) -> str:
    """Canonical seal over a change's FULL apply evidence (spec §1v5). Excludes fields that change
    legitimately during restore (restore_state, created_anomaly_deleted_at, post-restore status/
    error_code). Computed after post-flush; recomputed and compared before any restore write."""
    return _canonical_sha256({
        "evidence_schema_version": EVIDENCE_SCHEMA_VERSION,
        "plan_hash": plan_hash,
        "deterministic_action_id": ch.deterministic_action_id,
        "lane_fingerprint": ch.lane_fingerprint,
        "price_observation_id": ch.price_observation_id,
        "action_type": ch.action_type,
        "original_temporal_state": ch.original_temporal_state,
        "expected_bound_state": ch.expected_bound_state,
        "actual_after_state": ch.actual_after_state,
        "original_hash": ch.original_hash,
        "expected_bound_hash": ch.expected_bound_hash,
        "actual_after_hash": ch.actual_after_hash,
        "created_anomaly_original_id": ch.created_anomaly_original_id,
        "created_anomaly_hash": ch.created_anomaly_hash,
        "created_anomaly_live_id": ch.created_anomaly_live_id,
    })


def _run_execution_hash(run: HistoryRemediationRun,
                        changes: list[HistoryRemediationChange]) -> str:
    """Canonical seal over the WHOLE run (spec §2v5): the ordered per-change seals plus the run's
    post-apply evidence, provenance pair and backup evidence hash. Stored on both the run and the
    (immutable) plan-consumption row so a restore can prove nothing was altered."""
    return _canonical_sha256({
        "evidence_schema_version": EVIDENCE_SCHEMA_VERSION,
        "plan_hash": run.plan_hash,
        "apply_evidence_hashes": sorted(c.apply_evidence_hash for c in changes),
        "post_apply_occurrence_hashes": run.post_apply_occurrence_hashes,
        "post_apply_supported_fk_hashes": run.post_apply_supported_fk_hashes,
        "discovered_fk_fingerprint": run.discovered_fk_fingerprint,
        "expected_unknown_fk_count": run.expected_unknown_fk_count,
        "expected_commit_sha": run.expected_commit_sha,
        "observed_commit_sha": run.observed_commit_sha,
        "expected_source_hash": run.expected_source_hash,
        "observed_source_hash": run.observed_source_hash,
        "expected_api_artifact_hash": run.expected_api_artifact_hash,
        "observed_api_artifact_hash": run.observed_api_artifact_hash,
        "expected_worker_artifact_hash": run.expected_worker_artifact_hash,
        "observed_worker_artifact_hash": run.observed_worker_artifact_hash,
        "expected_provenance_document_hash": run.expected_provenance_document_hash,
        "observed_provenance_document_hash": run.observed_provenance_document_hash,
        "backup_evidence_hash": run.backup_evidence_hash,
        # Sealed authorization identity + expected backup (§1v2).
        "authorization_id": run.authorization_id,
        "authorization_package_hash": run.authorization_package_hash,
        "authorization_key_fingerprint": run.authorization_key_fingerprint,
        "authorization_generated_at": _iso_utc(run.authorization_generated_at),
        "authorization_expires_at": _iso_utc(run.authorization_expires_at),
        "expected_backup_sha256": run.expected_backup_sha256,
        "expected_backup_storage_reference_hash": run.expected_backup_storage_reference_hash,
    })


def _assert_counts_preserved(before: dict[str, int], after: dict[str, int]) -> None:
    if before["price_observation"] != after["price_observation"] or \
            before["price_observation_occurrence"] != after["price_observation_occurrence"]:
        raise ApplyPlanDrift("counts_not_preserved",
                             f"{before} != {after}")  # facts/occurrences must never change count


# --------------------------------------------------------------------------- #
# CLI — production allows ONLY read-only modes (spec §12; ceremony §5v2)
# --------------------------------------------------------------------------- #
# Stable, documented ceremony exit codes (§5v2):
EXIT_OK = 0             # full validation (prepared + written, or apply_ready=true)
EXIT_GATES_BLOCKING = 2  # a gate blocks (prepared=false / apply_ready=false)
EXIT_INVALID_INPUT = 3   # invalid file/evidence/package/signature/output
EXIT_UNEXPECTED = 4      # sanitized unexpected operational error


def _emit_ceremony_report(report: dict[str, Any], code: int) -> int:
    """Print the sanitized JSON report (never a path or secret) and return the exit code."""
    json.dump(report, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")
    return code


def _emit_ceremony_error(code: int) -> int:
    """Emit ONLY a sanitized error code — no traceback, no path, no secret."""
    msg = {EXIT_INVALID_INPUT: "invalid_ceremony_input"}.get(code, "unexpected_error")
    json.dump({"apply_ready": False, "error": msg}, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return code


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - thin CLI wrapper
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--manifest-path", required=True)
    p.add_argument("--operational-evidence-path")
    p.add_argument("--operator-reference")
    p.add_argument("--output-path")
    p.add_argument("--authorization-package-path")
    p.add_argument("--authorization-signature-path")
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--verify-only", action="store_true")
    mode.add_argument("--simulate", action="store_true")
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--restore", metavar="RUN_PUBLIC_ID")
    mode.add_argument("--prepare-authorization-request", action="store_true")
    mode.add_argument("--verify-authorization-ceremony", action="store_true")
    a = p.parse_args(argv)
    manifest = load_manifest(a.manifest_path)
    cloud = os.environ.get("DEPLOYMENT_MODE", "").lower() in ("cloud", "production")
    # apply/restore stay impossible from the CLI, in every phase and every mode (§8v5).
    if a.apply or a.restore:
        raise SystemExit(
            "ABORT: --apply/--restore are not authorized in this phase. Only read-only modes "
            "(--verify-only / --prepare-authorization-request / --verify-authorization-ceremony) "
            "may run against production.")
    if a.simulate and cloud:
        raise SystemExit(
            "ABORT: --simulate is not allowed in cloud/production; only verify-only runs here.")

    if a.prepare_authorization_request:
        if not a.operational_evidence_path or not a.operator_reference or not a.output_path:
            raise SystemExit("ABORT: prepare requires --operational-evidence-path, "
                             "--operator-reference and --output-path.")
        from cestaplan_api.provenance.operational_evidence import (
            CeremonyFileError,
            secure_create_request_file,
        )
        # §1v4: the CLI never injects a clock; the ceremony context loads observed evidence only.
        try:
            ctx = ApplyContext.from_ceremony_files(
                plan_hash=manifest.get("plan_hash") or "",
                operational_evidence_path=a.operational_evidence_path)
            with SessionLocal() as db:
                out = prepare_authorization_request(
                    db, manifest, ctx, operator_reference=a.operator_reference)
                db.rollback()  # read-only snapshot; NEVER a write
        except (CeremonyFileError, ApplyError):
            return _emit_ceremony_error(EXIT_INVALID_INPUT)  # sanitized; no path/traceback
        except Exception:
            return _emit_ceremony_error(EXIT_UNEXPECTED)
        if out.get("prepared") and out.get("request") is not None:
            payload = (_ceremony_canonical(out["request"]) + "\n").encode()  # canonical + newline
            try:  # §1v2: exclusive, symlink-free, fsynced create — never over an existing file
                secure_create_request_file(a.output_path, payload)
            except CeremonyFileError:
                return _emit_ceremony_report(
                    {"prepared": True, "output_written": False}, EXIT_INVALID_INPUT)
            return _emit_ceremony_report(
                {"prepared": True, "request_blockers": [], "output_written": True}, EXIT_OK)
        # Blocked by gates -> non-zero, and NEVER create the output file.
        return _emit_ceremony_report(
            {"prepared": False, "request_blockers": out.get("request_blockers", []),
             "output_written": False}, EXIT_GATES_BLOCKING)

    if a.verify_authorization_ceremony:
        if not a.operational_evidence_path or not a.authorization_package_path \
                or not a.authorization_signature_path:
            raise SystemExit("ABORT: verify-ceremony requires --operational-evidence-path, "
                             "--authorization-package-path and --authorization-signature-path.")
        from cestaplan_api.provenance.operational_evidence import CeremonyFileError
        try:
            ctx = ApplyContext.from_ceremony_files(
                plan_hash=manifest.get("plan_hash") or "",
                operational_evidence_path=a.operational_evidence_path,
                authorization_package_path=a.authorization_package_path,
                authorization_signature_path=a.authorization_signature_path)
            with SessionLocal() as db:
                out = verify_authorization_ceremony(db, manifest, ctx)
                db.rollback()
        except (CeremonyFileError, ApplyError):
            return _emit_ceremony_error(EXIT_INVALID_INPUT)
        except Exception:
            return _emit_ceremony_error(EXIT_UNEXPECTED)
        code = EXIT_OK if out.get("apply_ready") else EXIT_GATES_BLOCKING
        return _emit_ceremony_report(out, code)

    ctx = ApplyContext.from_environment(plan_hash=manifest.get("plan_hash"))
    with SessionLocal() as db:  # verify_only/simulate pin the read-only snapshot themselves (§10)
        out = verify_only(db, manifest, ctx) if a.verify_only else simulate(db, manifest, ctx)
        db.rollback()
    json.dump(out, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
