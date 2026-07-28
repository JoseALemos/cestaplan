"""Reversible history-lane remediation executor — real-PostgreSQL tests (apply spec §11 + v2).

The executor CONSUMES a sealed planner manifest and executes exactly the reviewed v1 plan (logical
rollbacks — never deletes — and deterministic interval reconstruction). Every gate fails closed with
zero writes; exact expected/observed provenance, real backup evidence, manifest-blocker resolution,
supported-FK drift, deterministic audit, durable failure/restore auditing, a connection-scoped write
guard and session/snapshot gates are all exercised.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import tempfile
import threading
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import delete, func, select, text
from sqlalchemy.orm import Session

from cestaplan_api.config import Settings
from cestaplan_api.db import engine
from cestaplan_api.models import (
    ExternalProduct,
    HistoryRemediationChange,
    HistoryRemediationPlanConsumption,
    HistoryRemediationRun,
    PriceAnomaly,
    PriceObservation,
    PriceObservationOccurrence,
    ProductVariant,
    PromotionRule,
    Retailer,
)
from cestaplan_api.tools import apply_history_lane_remediation as apply_tool
from cestaplan_api.tools import plan_history_lane_remediation as planner
from tests.fixtures.provider_scenarios import seed_test_catalog_product, seed_test_retailer

PROVIDER = "test_apply_provider"
T0 = datetime(2026, 7, 25, 8, 0, tzinfo=UTC)
T1 = datetime(2026, 7, 26, 8, 0, tzinfo=UTC)
T2 = datetime(2026, 7, 27, 8, 0, tzinfo=UTC)
_COMMIT = "4e43bad142b344274d7998cc80d54a708e118613"  # 40-hex
_SRC, _API, _WRK, _DOC = "a" * 64, "b" * 64, "c" * 64, "d" * 64  # 64-hex
_GENVER = "build-provenance-v1"
_BACKUP_REF = "s3://cestaplan-backups/history-remediation.dump"
_BACKUP_REF_HASH = hashlib.sha256(_BACKUP_REF.encode()).hexdigest()
# EPHEMERAL Ed25519 test key — NEVER a real production key. The §2v4 under-lock re-validation runs
# the FULL loader (canonical bytes, self-hash, Ed25519 verify against the trust-root, plan/temporal
# binding), so the executor tests seal REAL ephemeral packages against a REAL ephemeral trust-root.
def _mk_key() -> tuple[Ed25519PrivateKey, str, str]:
    sk = Ed25519PrivateKey.generate()
    pk_hex = sk.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()
    return sk, pk_hex, hashlib.sha256(bytes.fromhex(pk_hex)).hexdigest()[:16]


_SK, _PK_HEX, _AUTH_FP = _mk_key()
# A SECOND authorized key: lets a restore-binding test seal a validly-signed package with a
# DIFFERENT fingerprint (both keys are in the trust-root, so re-validation passes and the
# run-binding is what rejects the different fingerprint).
_SK2, _PK2_HEX, _AUTH_FP2 = _mk_key()
_AUTH_ID = "auth-test-2026-07-27-001"
# Fixed authorization validity window (computed once at import; < 1h old for the whole suite) so an
# apply and its restore seal the byte-IDENTICAL package — the restore binding needs exact equality.
_AUTH_GEN = datetime.now(UTC) - timedelta(minutes=1)
_AUTH_EXP = datetime.now(UTC) + timedelta(hours=1)
CONFIRM = ("I_UNDERSTAND_THIS_WRITES", "PLAN_REVIEWED", "BACKUP_VERIFIED")
RESTORE_CONFIRM = ("I_UNDERSTAND_THIS_RESTORES", "RUN_REVIEWED")

_BACKUP: dict[str, Any] = {}
_AUTH: dict[str, Any] = {}  # per-test trust-root path/hash + package/signature paths


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


@pytest.fixture(autouse=True)
def _bake_trust_root(tmp_path_factory):
    """A REAL ephemeral trust-root (the test key) + fixed package/signature paths for THIS test.
    Uses os.environ directly (not monkeypatch) so a test's own monkeypatch.undo() cannot clear it.
    §2v4 under-lock re-validation reads these paths; a test that never seals a package leaves them
    absent, so the authorization gates fail closed."""
    d = tmp_path_factory.mktemp("authpkg")
    tr = d / "authorization-trust-root.json"
    tr.write_text(_canonical(
        {"authorized_ed25519_public_keys": [_PK_HEX, _PK2_HEX], "schema_version": 1}) + "\n")
    _AUTH.clear()
    _AUTH.update(
        trust_root_path=str(tr),
        trust_hash=hashlib.sha256(tr.read_bytes()).hexdigest(),
        pkg_path=str(d / "pkg.json"), sig_path=str(d / "pkg.sig"), fp=_AUTH_FP)
    prev = {k: os.environ.get(k) for k in (
        "BUILD_AUTHORIZATION_TRUST_ROOT_PATH", "AUTHORIZATION_PACKAGE_PATH",
        "AUTHORIZATION_SIGNATURE_PATH", "CESTAPLAN_PG_RESTORE_PATH")}
    os.environ["BUILD_AUTHORIZATION_TRUST_ROOT_PATH"] = _AUTH["trust_root_path"]
    os.environ["AUTHORIZATION_PACKAGE_PATH"] = _AUTH["pkg_path"]
    os.environ["AUTHORIZATION_SIGNATURE_PATH"] = _AUTH["sig_path"]
    os.environ["CESTAPLAN_PG_RESTORE_PATH"] = _BACKUP["pg_restore_path"]  # the FAKE pg 18 client
    try:
        yield
    finally:
        for k, v in prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# Back-compat alias: the observed-document trust-root hash equals the LIVE (ephemeral) trust-root.
def _trust_hash() -> str:
    return _AUTH["trust_hash"]


def _seal_ctx_package(ctx: Any, plan_hash: str, *, sk: Ed25519PrivateKey | None = None,
                      fp: str | None = None) -> str:
    """Build + sign an EPHEMERAL authorization package matching ``ctx`` and ``plan_hash`` EXACTLY,
    write it to the env paths, and set ctx.authorization_package_hash + fingerprint. The test
    analogue of a signed production package: it lets the full §2v4 under-lock re-validation pass for
    a legitimately-authorized ctx (substitution tests tamper with the file afterwards to fail it).
    Deterministic given the (fixed) validity window + expected values, so an apply and its restore
    seal the identical package and the restore binding matches. ``sk``/``fp`` sign with a specific
    authorized key (default the primary). Returns the self-hash."""
    sk = sk or _SK
    fp = fp or _AUTH_FP
    e = ctx.expected_provenance
    pkg = {
        "schema_version": 1,
        "authorization_id": ctx.authorization_id,
        "plan_hash": plan_hash,
        "main_commit_sha": ctx.expected_main_sha,
        "alembic_revision": ctx.expected_alembic,
        "expected_commit_sha": e.commit_sha,
        "expected_source_hash": e.source_tree_hash,
        "expected_api_artifact_hash": e.api_artifact_hash,
        "expected_worker_artifact_hash": e.worker_artifact_hash,
        "expected_document_hash": e.document_hash,
        "expected_product_price": ctx.expected_product_price,
        "expected_active_mappings": ctx.expected_active_mappings,
        "generated_at": ctx.authorization_generated_at.isoformat(),
        "expires_at": ctx.authorization_expires_at.isoformat(),
        "operator_reference": ctx.operator_reference,
        "backup_expected_sha256": ctx.expected_backup_sha256,
        "backup_storage_reference": ctx.expected_backup_storage_reference,
    }
    pkg["authorization_package_hash"] = hashlib.sha256(_canonical(pkg).encode()).hexdigest()
    body = (_canonical(pkg) + "\n").encode()
    Path(_AUTH["pkg_path"]).write_bytes(body)
    Path(_AUTH["sig_path"]).write_text(sk.sign(body).hex())
    ctx.authorization_package_hash = pkg["authorization_package_hash"]
    ctx.authorization_key_fingerprint = fp
    return pkg["authorization_package_hash"]


_FAKE_PG_RESTORE = (
    "#!/bin/sh\n"
    'case "$1" in\n'
    '  --version) echo "pg_restore (PostgreSQL) 18.4";;\n'
    '  --list) echo "; Dumped from database version: 18";;\n'
    "  *) exit 1;;\n"
    "esac\n"
    "exit 0\n"
)


def _relaxed_verify_pg_restore_binary(expected_sha256, *, expected_major, path):
    """Test double for apply_tool._verify_pg_restore_binary. Keeps the full fail-closed contract
    (regular file, no symlink, not group/other writable, SHA==doc, --version major==18) EXCEPT the
    strict root-owned gate is relaxed to 'root OR the current euid', so the backup happy paths run
    under a NON-root CI runner. The strict root-owned gate is covered directly by the root-only unit
    tests in test_pg_restore_binary_verification.py and by CI's image-runtime job."""
    import stat as _s
    if not isinstance(expected_sha256, str) or not apply_tool._SHA256_RE.match(expected_sha256):
        return False, None
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
    except OSError:
        return False, None
    try:
        st = os.fstat(fd)
        if (_s.S_ISLNK(st.st_mode) or not _s.S_ISREG(st.st_mode)
                or st.st_uid not in (0, os.geteuid()) or (st.st_mode & 0o022)):
            return False, None
        h = hashlib.sha256()
        while chunk := os.read(fd, 1 << 20):
            h.update(chunk)
        if h.hexdigest() != expected_sha256:
            return False, None
    finally:
        os.close(fd)
    ver = subprocess.run([path, "--version"], capture_output=True, text=True,
                         errors="replace", timeout=30, check=False)
    if ver.returncode != 0 or apply_tool._major(ver.stdout.strip()) != expected_major:
        return False, None
    return True, path


@pytest.fixture(scope="module", autouse=True)
def _module_backup():
    """A REAL pg_dump (custom, schema-only) + a FAKE pinned pg 18 client (a 0755 script, major 18,
    echoing a version-18 --list) so BackupEvidence.verify()'s binary + version checks pass without a
    real pg 18 install. The strict root-owned gate is relaxed via a test double (see above); the
    real client + root-owned gate are exercised in CI's image-runtime job. Everything is normalized
    to major 18 (evidence + a stubbed server_version) so the compatibility gate holds.
    """
    fd, path = tempfile.mkstemp(suffix=".dump")
    os.close(fd)
    uri = Settings().database_url.replace("+psycopg", "")
    subprocess.run(["pg_dump", "-Fc", "--schema-only", "--dbname", uri, "-f", path],
                   check=True, capture_output=True, timeout=120)
    os.chmod(path, 0o600)
    prfd, prpath = tempfile.mkstemp(suffix="_pg_restore")
    os.write(prfd, _FAKE_PG_RESTORE.encode())
    os.close(prfd)
    os.chmod(prpath, 0o755)  # root-owned (tests run as root), not group/other writable
    _BACKUP["path"] = path
    _BACKUP["sha256"] = hashlib.sha256(Path(path).read_bytes()).hexdigest()
    _BACKUP["pg_major"] = "18"  # simulate a pg 18 runtime (the binary contract requires major 18)
    _BACKUP["pg_restore_path"] = prpath
    _BACKUP["pg_restore_sha256"] = hashlib.sha256(Path(prpath).read_bytes()).hexdigest()
    real_server_version = apply_tool._server_version
    apply_tool._server_version = lambda db: "18"  # normalize the observed DB major to 18
    real_verify = apply_tool._verify_pg_restore_binary
    apply_tool._verify_pg_restore_binary = _relaxed_verify_pg_restore_binary
    yield
    apply_tool._verify_pg_restore_binary = real_verify
    apply_tool._server_version = real_server_version
    os.unlink(path)
    os.unlink(prpath)


def _backup_evidence(now: datetime) -> Any:
    # expected_postgres_version pins the server major so BackupEvidence.verify() can prove the
    # pg_restore / dump / live-server versions are all compatible (§7), not just assumed.
    return apply_tool.BackupEvidence(
        path=_BACKUP["path"], expected_sha256=_BACKUP["sha256"], created_at=now,
        expected_postgres_version=_BACKUP["pg_major"],
        storage_reference="s3://cestaplan-backups/history-remediation.dump")


def _fixture(db: Session):
    retailer = seed_test_retailer(db, PROVIDER)
    _p, variant = seed_test_catalog_product(db, retailer, "AP-1", name="Apply", price=None)
    return retailer, variant


def _obs(db, rid, vid, *, amount, observed_at=T0, valid_until=None, status="unverified"):
    o = PriceObservation(
        retailer_id=rid, product_variant_id=vid, price_scope="national", price_type="regular",
        amount=Decimal(amount), currency="EUR", requires_loyalty=False, observed_at=observed_at,
        imported_at=observed_at, valid_from=observed_at, valid_until=valid_until,
        confidence_score=Decimal("1.0"), staging_only=True, verification_status=status)
    db.add(o)
    db.flush()
    return o


def _dup_lane(db, rid, vid):
    """Two exact duplicates at T0 -> a plannable lane (1 canonical keep + 1 logical rollback)."""
    a = _obs(db, rid, vid, amount="1.19", observed_at=T0)
    b = _obs(db, rid, vid, amount="1.19", observed_at=T0)
    return a, b


def _make_manifest(db: Session, provider: str = PROVIDER) -> dict:
    return json.loads(json.dumps(
        planner._dry_run_in_snapshot(db, provider)["manifest"], default=str))


def _alembic(db: Session) -> str | None:
    return db.execute(text("SELECT version_num FROM alembic_version")).scalar()


def _live_counts(db: Session) -> tuple[int, int]:
    from cestaplan_api.models import ProductPrice, ProviderIngredientMapping
    pp = int(db.scalar(select(func.count()).select_from(ProductPrice)) or 0)
    mp = int(db.scalar(select(func.count()).select_from(ProviderIngredientMapping).where(
        ProviderIngredientMapping.active.is_(True))) or 0)
    return pp, mp


def _ctx(db: Session, *, backup: Any = "default", m: Any = None, **over):
    pp, mp = _live_counts(db)
    now = datetime.now(UTC)
    live_alembic = _alembic(db)
    _exp = apply_tool.ExpectedProvenance(_COMMIT, _SRC, _API, _WRK, _DOC)
    # A FULL observed document (alembic + generator + trust-root) so the build-identity gates pass.
    # The observed trust-root hash equals the LIVE (ephemeral) trust-root the fixture baked.
    obs = apply_tool.BuildProvenance(
        _COMMIT, _SRC, _API, _WRK, _DOC, alembic_revision=live_alembic,
        generator_version=_GENVER, authorization_trust_root_hash=_AUTH["trust_hash"],
        pg_restore_binary_sha256=_BACKUP["pg_restore_sha256"], pg_restore_major="18")
    be = _backup_evidence(now) if backup == "default" else backup
    base = {
        "app_commit_sha": _COMMIT, "deployed_api_sha": _COMMIT, "deployed_worker_sha": _COMMIT,
        "expected_main_sha": _COMMIT, "expected_alembic": live_alembic,
        "observed_provenance": obs, "expected_provenance": _exp,
        "expected_product_price": pp, "expected_active_mappings": mp, "backup": be,
        # Backup binding — as if fed by a signed package (the internal explicit API; §1v2).
        "expected_backup_sha256": _BACKUP["sha256"],
        "expected_backup_storage_reference": _BACKUP_REF,
        "expected_backup_storage_reference_hash": _BACKUP_REF_HASH,
        # Verified authorization identity (§3v3). authorization_package_hash + fingerprint are set
        # for real when the package is sealed (by _bind_plan / the _apply/_verify/_restore helpers);
        # a valid-sha256 placeholder keeps the identity gate well-formed before sealing.
        "authorization": {"package_present": True, "signature_valid": True},
        "authorization_id": _AUTH_ID, "authorization_package_hash": "0" * 64,
        "authorization_key_fingerprint": _AUTH["fp"],
        "authorization_generated_at": _AUTH_GEN,
        "authorization_expires_at": _AUTH_EXP,
        "authorization_valid": True,
        "authorization_plan_hash": m["plan_hash"] if m else None,
        "operator_reference": "ticket-OPS-1", "now": now}
    base.update(over)
    ctx = apply_tool.ApplyContext(**base)
    # When the manifest is known AND the authorization is meant to be valid, seal the real ephemeral
    # package now (tests that call _apply_guarded directly rely on this; the _apply/_verify/_restore
    # helpers seal via _bind_plan when m is not supplied here).
    if m is not None and getattr(ctx, "authorization_valid", False):
        _seal_ctx_package(ctx, m["plan_hash"])
    return ctx


def _bind_plan(ctx, plan_hash):
    """Bind the verified authorization to a plan_hash and SEAL a real ephemeral package matching ctx
    (so the §2v4 under-lock re-validation passes). Sealing is deterministic — an apply and its
    restore produce the identical package, satisfying the exact restore binding."""
    if getattr(ctx, "authorization_valid", False):
        if ctx.authorization_plan_hash is None:
            ctx.authorization_plan_hash = plan_hash
        # Seal only while the package hash is the unsealed placeholder — a test that manually sealed
        # (e.g. with the second key, for a fingerprint-binding case) is left untouched.
        if ctx.authorization_plan_hash == plan_hash and ctx.authorization_package_hash in (
                None, "0" * 64):
            _seal_ctx_package(ctx, plan_hash)
    return ctx


def _verify(db, m, ctx):  # bypasses the public snapshot-pinning for the seeded db_session fixture
    return apply_tool._verify_report(db, m, _bind_plan(ctx, m["plan_hash"]))


def _blocking(db, m, ctx):
    return _verify(db, m, ctx)["gates_blocking"]


def _apply(db, m, ctx, **kw):
    # The db_session fixture runs inside an outer transaction (savepoint isolation), so the public
    # virgin-session gate would reject it. Behavioural tests exercise the guarded entrypoint, which
    # enforces every real gate; the public virgin gate is covered separately (§6 session tests).
    kw.setdefault("authorized", True)
    kw.setdefault("confirmations", CONFIRM)
    return apply_tool._apply_guarded(db, m, _bind_plan(ctx, m["plan_hash"]), **kw)


def _counts(db, rid):
    obs = int(db.scalar(select(func.count()).select_from(PriceObservation).where(
        PriceObservation.retailer_id == rid)) or 0)
    occ = int(db.scalar(select(func.count()).select_from(PriceObservationOccurrence).join(
        PriceObservation, PriceObservation.id == PriceObservationOccurrence.price_observation_id
    ).where(PriceObservation.retailer_id == rid)) or 0)
    return obs, occ


def _tamper(m: dict, mutate) -> dict:
    m2 = copy.deepcopy(m)
    mutate(m2)
    return m2


def _raise(exc):
    raise exc


# --------------------------------------------------------------------------- #
# §11.1 verify-only + §11.2 simulate
# --------------------------------------------------------------------------- #
def test_verify_only_all_green_is_apply_ready(db_session: Session) -> None:
    r, v = _fixture(db_session)
    _dup_lane(db_session, r.id, v.id)
    m = _make_manifest(db_session)
    rep = _verify(db_session, m, _ctx(db_session))
    assert rep["gates_blocking"] == [] and rep["apply_blockers"] == []
    assert rep["apply_ready"] is True  # everything supplied -> ready (prod still lacks it -> false)
    assert "plan_hash_intact" in rep["gates_passed"]


def test_verify_only_blocks_without_immutable_build(db_session: Session) -> None:  # §12 / §11.15
    r, v = _fixture(db_session)
    _dup_lane(db_session, r.id, v.id)
    m = _make_manifest(db_session)
    rep = _verify(db_session, m,
                  _ctx(db_session, observed_provenance=apply_tool.BuildProvenance()))
    assert rep["apply_ready"] is False
    assert "immutable_build_provenance_missing" in rep["apply_blockers"]
    assert "immutable_build_provenance" in rep["gates_blocking"]


def test_public_verify_only_pins_readonly_snapshot() -> None:  # §10
    s = Session(bind=engine.connect(), expire_on_commit=False)
    try:
        slug, rid = _seed_committed()
        try:
            m = _manifest_committed(slug)
            probe = _isession()  # build ctx from a throwaway session so `s` stays pristine
            ctx = _ctx(probe, m=m)
            probe.close()
            apply_tool.verify_only(s, m, ctx)  # pins the snapshot as its first statement on `s`
            assert s.execute(text("SHOW transaction_read_only")).scalar() == "on"
            with pytest.raises(Exception):  # noqa: B017 - read-only rejects a write
                s.execute(text("INSERT INTO retailer (slug, name, adapter_key, public_id) "
                               "VALUES ('x','x','x', gen_random_uuid())"))
            s.rollback()
        finally:
            _cleanup(rid)
    finally:
        s.close()


def test_simulate_over_many_groups(db_session: Session) -> None:  # §11.2
    r, _v = _fixture(db_session)
    for i in range(49):
        _p, vi = seed_test_catalog_product(db_session, r, f"AP-G{i}", name=f"G{i}", price=None)
        _obs(db_session, r.id, vi.id, amount="1.19", observed_at=T0)
        _obs(db_session, r.id, vi.id, amount="1.19", observed_at=T0)
    m = _make_manifest(db_session)
    rep = apply_tool._simulate_report(db_session, m, _ctx(db_session))
    assert rep["simulated_invariants_ok"] is True and rep["planned_changes"] == 49


# --------------------------------------------------------------------------- #
# §11.3/§11.4/§11.6 apply + deterministic audit (§4)
# --------------------------------------------------------------------------- #
def test_apply_logical_rollback_no_delete(db_session: Session) -> None:  # §11.3
    r, v = _fixture(db_session)
    a, b = _dup_lane(db_session, r.id, v.id)
    before = _counts(db_session, r.id)
    m = _make_manifest(db_session)
    res = _apply(db_session, m, _ctx(db_session))
    assert res["status"] == "applied" and _counts(db_session, r.id) == before
    rolled = [o for o in (a, b) if (db_session.refresh(o) or o.rolled_back_at is not None)]
    assert len(rolled) == 1


def test_apply_reconstructs_intervals(db_session: Session) -> None:  # §11.4
    r, v = _fixture(db_session)
    a = _obs(db_session, r.id, v.id, amount="1.19", observed_at=T0)
    _obs(db_session, r.id, v.id, amount="1.29", observed_at=T1)
    m = _make_manifest(db_session)
    _apply(db_session, m, _ctx(db_session))
    db_session.refresh(a)
    assert a.valid_until == T1


def test_apply_writes_deterministic_audit(db_session: Session) -> None:  # §11.6 + §4
    r, v = _fixture(db_session)
    _dup_lane(db_session, r.id, v.id)
    m = _make_manifest(db_session)
    _apply(db_session, m, _ctx(db_session))
    run = db_session.execute(select(HistoryRemediationRun).where(
        HistoryRemediationRun.plan_hash == m["plan_hash"])).scalar_one()
    assert run.status == "applied" and run.observed_source_hash == _SRC
    assert run.expected_source_hash == _SRC
    assert run.observed_provenance_document_hash == _DOC
    assert run.expected_provenance_document_hash == _DOC
    changes = db_session.execute(select(HistoryRemediationChange).where(
        HistoryRemediationChange.remediation_run_id == run.id)).scalars().all()
    assert len(changes) == 2
    for ch in changes:
        assert len(ch.deterministic_action_id) == 64 and ch.lane_fingerprint
        assert ch.actual_after_hash is not None and ch.status == "applied"
        if ch.action_type in apply_tool._ACTION_WRITES:
            assert ch.actual_after_hash == ch.expected_bound_hash
    ids = {ch.deterministic_action_id for ch in changes}
    assert len(ids) == 2  # unique per (run, action)


# --------------------------------------------------------------------------- #
# §11.5 exact restore + §11.26 restore repeated
# --------------------------------------------------------------------------- #
def _restore(db, run_id, ctx, **kw):
    kw.setdefault("authorized", True)
    kw.setdefault("confirmations", RESTORE_CONFIRM)
    plan = db.execute(select(HistoryRemediationRun.plan_hash).where(
        HistoryRemediationRun.public_id == run_id)).scalar()
    if plan is not None:
        _bind_plan(ctx, plan)
    return apply_tool._restore_guarded(db, run_id, ctx, **kw)


def test_exact_restore_round_trips(db_session: Session) -> None:  # §11.5
    r, v = _fixture(db_session)
    a, b = _dup_lane(db_session, r.id, v.id)
    originals = {o.id: (o.valid_from, o.valid_until, o.verification_status, o.rolled_back_at)
                 for o in (a, b)}
    m = _make_manifest(db_session)
    res = _apply(db_session, m, _ctx(db_session))
    rest = _restore(db_session, res["run_public_id"], _ctx(db_session))
    assert rest["status"] == "restored"
    for o in (a, b):
        db_session.refresh(o)
        assert (o.valid_from, o.valid_until, o.verification_status,
                o.rolled_back_at) == originals[o.id]


def test_restore_repeated_is_idempotent(db_session: Session) -> None:  # §11.26
    r, v = _fixture(db_session)
    _dup_lane(db_session, r.id, v.id)
    m = _make_manifest(db_session)
    res = _apply(db_session, m, _ctx(db_session))
    _restore(db_session, res["run_public_id"], _ctx(db_session))
    again = _restore(db_session, res["run_public_id"], _ctx(db_session))
    assert again["status"] == "already_restored"


# --------------------------------------------------------------------------- #
# §7/§11 anomaly lifecycle via a synthetic sealed side effect (mark_disputed is out in v1)
# --------------------------------------------------------------------------- #
def _manifest_with_anomaly(db) -> tuple:
    r, v = _fixture(db)
    _dup_lane(db, r.id, v.id)
    m = _make_manifest(db)
    lane = next(x for x in m["lanes"] if not x["excluded"])
    target = lane["rows"][0]["integrity"]["full_row_hash"]
    lane["proposed_side_effects"] = [{
        "type": "create_price_anomaly", "anomaly_type": planner._SAME_TIMESTAMP_CONFLICT,
        "severity": "high", "target_observation_ref": target, "original_state": "absent",
        "restore_action": "delete_only_created_row",
        "expected_payload_template": {"status": "open"}, "deterministic_action_id": "syn"}]
    m["plan_hash"] = apply_tool._recompute_plan_hash(m)  # re-seal so plan_hash_intact holds
    return m, r


def test_anomaly_created_then_removed_only_by_restore(db_session: Session) -> None:  # §11.27/§28/§7
    pre = PriceAnomaly(anomaly_type="preexisting", severity="low", status="open")
    db_session.add(pre)
    db_session.flush()
    m, _r = _manifest_with_anomaly(db_session)
    res = _apply(db_session, m, _ctx(db_session))
    run = db_session.execute(select(HistoryRemediationRun).where(
        HistoryRemediationRun.plan_hash == m["plan_hash"])).scalar_one()
    created = [c.created_anomaly_original_id for c in db_session.execute(select(
        HistoryRemediationChange).where(
        HistoryRemediationChange.remediation_run_id == run.id,
        HistoryRemediationChange.created_anomaly_original_id.is_not(None))).scalars()]
    assert len(created) == 1
    _restore(db_session, res["run_public_id"], _ctx(db_session))
    assert db_session.get(PriceAnomaly, created[0]) is None  # the run's anomaly deleted
    assert db_session.get(PriceAnomaly, pre.id) is not None  # preexisting preserved
    ch = db_session.execute(select(HistoryRemediationChange).where(
        HistoryRemediationChange.created_anomaly_original_id == created[0])).scalar_one()
    assert ch.created_anomaly_original_id == created[0]  # durable reference kept after delete
    assert ch.created_anomaly_live_id is None and ch.created_anomaly_deleted_at is not None


# --------------------------------------------------------------------------- #
# §11.7 mid-failure durable failed run + retry link (§5) + §11.25 idempotency
# --------------------------------------------------------------------------- #
def test_mid_failure_records_durable_failed_run(db_session: Session, monkeypatch) -> None:  # §5
    r, v = _fixture(db_session)
    _dup_lane(db_session, r.id, v.id)
    m = _make_manifest(db_session)
    monkeypatch.setattr(apply_tool, "_apply_row",
                        lambda *a, **k: _raise(apply_tool.ApplyPlanDrift("injected")))
    try:
        with pytest.raises(apply_tool.ApplyPlanDrift):
            _apply(db_session, m, _ctx(db_session))
        failed = _committed_runs(m["plan_hash"], status="failed")
        assert len(failed) == 1 and failed[0]["error_code"] == "injected"
    finally:
        _delete_runs(m["plan_hash"])


def test_apply_repeated_returns_already_applied(db_session: Session) -> None:  # §11.25
    r, v = _fixture(db_session)
    _dup_lane(db_session, r.id, v.id)
    m = _make_manifest(db_session)
    _apply(db_session, m, _ctx(db_session))
    assert _apply(db_session, m, _ctx(db_session))["status"] == "already_applied"


def _committed_runs(plan_hash: str, *, status: str) -> list[dict]:
    s = _isession()
    try:
        return [{"error_code": rr.error_code, "id": rr.id} for rr in s.execute(select(
            HistoryRemediationRun).where(HistoryRemediationRun.plan_hash == plan_hash,
                                         HistoryRemediationRun.status == status)).scalars()]
    finally:
        s.close()


def _delete_runs(plan_hash: str) -> None:
    s = _isession()
    try:
        rids = s.execute(select(HistoryRemediationRun.id).where(
            HistoryRemediationRun.plan_hash == plan_hash)).scalars().all()
        if rids:
            s.execute(delete(HistoryRemediationChange).where(
                HistoryRemediationChange.remediation_run_id.in_(rids)))
            s.execute(delete(HistoryRemediationPlanConsumption).where(
                HistoryRemediationPlanConsumption.first_run_id.in_(rids)))
            s.execute(delete(HistoryRemediationRun).where(HistoryRemediationRun.id.in_(rids)))
        s.commit()
    finally:
        s.close()


# --------------------------------------------------------------------------- #
# §11.8-§11.12 tamper + drift (rows/occurrence/FK)
# --------------------------------------------------------------------------- #
def test_plan_hash_tamper_blocks(db_session: Session) -> None:  # §11.8
    r, v = _fixture(db_session)
    _dup_lane(db_session, r.id, v.id)
    m = _make_manifest(db_session)
    bad = _tamper(m, lambda x: x.update(plan_hash="deadbeef"))
    assert "plan_hash_intact" in _blocking(db_session, bad, _ctx(db_session))
    with pytest.raises(apply_tool.ApplyError):
        _apply(db_session, bad, _ctx(db_session))
    _delete_runs(bad["plan_hash"])


def test_original_hash_tamper_blocks(db_session: Session) -> None:  # §11.9
    r, v = _fixture(db_session)
    _dup_lane(db_session, r.id, v.id)
    m = _make_manifest(db_session)
    bad = _tamper(m, lambda x: x["lanes"][0]["rows"][0]["integrity"].update(full_row_hash="0" * 64))
    b = _blocking(db_session, bad, _ctx(db_session))
    assert "plan_hash_intact" in b or "row_hashes_match" in b


def test_occurrence_added_after_plan_blocks(db_session: Session) -> None:  # §11.10
    r, v = _fixture(db_session)
    a, _b = _dup_lane(db_session, r.id, v.id)
    m = _make_manifest(db_session)
    db_session.add(PriceObservationOccurrence(price_observation_id=a.id, provider_code="late",
                                              imported_at=T0))
    db_session.flush()
    assert "occurrences_unchanged" in _blocking(db_session, m, _ctx(db_session))


def test_unknown_fk_after_plan_blocks(db_session: Session) -> None:  # §11.11/§11.12
    r, v = _fixture(db_session)
    a, _b = _dup_lane(db_session, r.id, v.id)
    m = _make_manifest(db_session)
    db_session.execute(text("CREATE TABLE ap_unknown_ref (id bigint PRIMARY KEY, "
                            "obs_id bigint REFERENCES price_observation(id))"))
    db_session.execute(text("INSERT INTO ap_unknown_ref (id, obs_id) VALUES (1, :o)"), {"o": a.id})
    db_session.flush()
    assert "no_unknown_fk" in _blocking(db_session, m, _ctx(db_session))


def test_supported_fk_change_after_plan_blocks(db_session: Session) -> None:  # §3
    r, v = _fixture(db_session)
    a, _b = _dup_lane(db_session, r.id, v.id)
    m = _make_manifest(db_session)  # sealed with NO promotion rule
    db_session.add(PromotionRule(price_observation_id=a.id, type="percentage"))  # added after plan
    db_session.flush()
    assert "supported_fk_unchanged" in _blocking(db_session, m, _ctx(db_session))


# --------------------------------------------------------------------------- #
# §11.13-§11.20 contract / provenance / env gates
# --------------------------------------------------------------------------- #
def _dup_manifest(db):
    r, v = _fixture(db)
    _dup_lane(db, r.id, v.id)
    return r, _make_manifest(db)


def test_wrong_writer_contract_blocks(db_session: Session, monkeypatch) -> None:  # §11.13
    _r, m = _dup_manifest(db_session)
    monkeypatch.setattr(apply_tool.writer, "writer_contract",
                        lambda: {"version": "old", "active_exact_ambiguity_policy": "pick"})
    assert "writer_contract_v2" in _blocking(db_session, m, _ctx(db_session))


def test_wrong_app_commit_sha_blocks(db_session: Session) -> None:  # §11.14
    _r, m = _dup_manifest(db_session)
    b = _blocking(db_session, m, _ctx(db_session, app_commit_sha="b" * 40,
                                      deployed_api_sha="b" * 40, deployed_worker_sha="b" * 40))
    assert "main_commit_sha_matches" in b


def test_api_worker_misaligned_blocks(db_session: Session) -> None:  # §11.16
    _r, m = _dup_manifest(db_session)
    assert "api_worker_aligned" in _blocking(
        db_session, m, _ctx(db_session, deployed_worker_sha="f" * 40))


def test_wrong_alembic_blocks(db_session: Session) -> None:  # §11.17
    _r, m = _dup_manifest(db_session)
    assert "alembic_revision" in _blocking(db_session, m, _ctx(db_session, expected_alembic="nope"))


def test_active_provider_blocks(db_session: Session, monkeypatch) -> None:  # §11.18
    _r, m = _dup_manifest(db_session)
    monkeypatch.setattr(apply_tool, "_production_enabled", lambda s, a: True)
    assert "production_disabled" in _blocking(db_session, m, _ctx(db_session))


def test_active_crawl_run_blocks(db_session: Session) -> None:  # §11.19
    r, m = _dup_manifest(db_session)
    from cestaplan_api.models import CrawlRun
    db_session.add(CrawlRun(retailer_id=r.id, run_type="prices", status="running"))
    db_session.flush()
    assert "crawl_run_not_running" in _blocking(db_session, m, _ctx(db_session))


def test_active_crawl_job_blocks(db_session: Session) -> None:  # §11.20
    r, m = _dup_manifest(db_session)
    from cestaplan_api.models import CrawlJob, CrawlRun
    run = CrawlRun(retailer_id=r.id, run_type="prices", status="completed")
    db_session.add(run)
    db_session.flush()
    db_session.add(CrawlJob(crawl_run_id=run.id, job_type="prices", status="queued"))
    db_session.flush()
    assert "crawl_job_not_active" in _blocking(db_session, m, _ctx(db_session))


def test_missing_backup_blocks(db_session: Session) -> None:  # §9
    _r, m = _dup_manifest(db_session)
    assert "backup_verified" in _blocking(db_session, m, _ctx(db_session, backup=None))


def test_bad_backup_sha_blocks(db_session: Session) -> None:  # §9
    _r, m = _dup_manifest(db_session)
    bad = apply_tool.BackupEvidence(path=_BACKUP["path"], expected_sha256="e" * 64,
                                    created_at=datetime.now(UTC))
    assert "backup_verified" in _blocking(db_session, m, _ctx(db_session, backup=bad))


# --------------------------------------------------------------------------- #
# §1 provenance — 7 exact-comparison cases
# --------------------------------------------------------------------------- #
def _prov_blocking(db, m, *, observed=None, expected=None):
    # Only override observed/expected when a test supplies one; otherwise use the FULL default ctx
    # (so the build-identity gates pass and provenance_exact yields no blockers).
    over: dict[str, Any] = {}
    if observed is not None:
        over["observed_provenance"] = observed
    if expected is not None:
        over["expected_provenance"] = expected
    return _blocking(db, m, _ctx(db, **over))


def test_provenance_absent_blocks(db_session: Session) -> None:  # §1.1
    _r, m = _dup_manifest(db_session)
    assert "immutable_build_provenance" in _prov_blocking(
        db_session, m, observed=apply_tool.BuildProvenance())


def test_provenance_malformed_blocks(db_session: Session) -> None:  # §1.2
    _r, m = _dup_manifest(db_session)
    bad = apply_tool.BuildProvenance("zz", "short", "x", "y", "notahash")
    assert "immutable_build_provenance" in _prov_blocking(db_session, m, observed=bad)


def test_provenance_source_hash_differs_blocks(db_session: Session) -> None:  # §1.3
    _r, m = _dup_manifest(db_session)
    diff = apply_tool.BuildProvenance(_COMMIT, "9" * 64, _API, _WRK, _DOC)
    b = _prov_blocking(db_session, m, observed=diff)
    assert "provenance_source_matches" in b and "immutable_build_provenance" in b


def test_provenance_commit_differs_blocks(db_session: Session) -> None:  # §1.4
    _r, m = _dup_manifest(db_session)
    diff = apply_tool.BuildProvenance("b" * 40, _SRC, _API, _WRK, _DOC)
    assert "provenance_commit_matches" in _prov_blocking(db_session, m, observed=diff)


def test_provenance_api_ok_worker_bad_blocks(db_session: Session) -> None:  # §1.5
    _r, m = _dup_manifest(db_session)
    diff = apply_tool.BuildProvenance(_COMMIT, _SRC, _API, "9" * 64, _DOC)
    b = _prov_blocking(db_session, m, observed=diff)
    assert "provenance_worker_artifact_matches" in b
    assert "provenance_api_artifact_matches" not in b  # api still matches


def test_provenance_same_commit_diff_artifact_blocks(db_session: Session) -> None:  # §1.6
    _r, m = _dup_manifest(db_session)
    diff = apply_tool.BuildProvenance(_COMMIT, _SRC, "9" * 64, _WRK, _DOC)
    assert "provenance_api_artifact_matches" in _prov_blocking(db_session, m, observed=diff)


def test_provenance_exact_passes(db_session: Session) -> None:  # §1.7
    _r, m = _dup_manifest(db_session)
    assert _prov_blocking(db_session, m) == []  # everything matches -> zero provenance blockers


# --------------------------------------------------------------------------- #
# §2 manifest-blockers resolution policy
# --------------------------------------------------------------------------- #
def test_manifest_blockers_resolved_reported(db_session: Session) -> None:  # §2
    _r, m = _dup_manifest(db_session)
    rep = _verify(db_session, m, _ctx(db_session))
    assert set(m["apply_blockers"]) <= set(rep["manifest_blockers_present"])
    assert rep["blockers_unresolved"] == [] and "planner_is_plan_only" in rep["blockers_resolved"]


def test_unknown_manifest_blocker_fails_closed(db_session: Session) -> None:  # §2
    _r, m0 = _dup_manifest(db_session)
    m = _tamper(m0, lambda x: x["apply_blockers"].append("some_unknown_blocker"))
    m["plan_hash"] = apply_tool._recompute_plan_hash(m)
    b = _blocking(db_session, m, _ctx(db_session))
    assert "manifest_blockers_resolved" in b


# --------------------------------------------------------------------------- #
# §11 v1 scope: mark_disputed blocked
# --------------------------------------------------------------------------- #
def test_mark_disputed_action_blocked_in_v1(db_session: Session) -> None:  # §11
    r, v = _fixture(db_session)
    _obs(db_session, r.id, v.id, amount="1.19", observed_at=T0)
    _obs(db_session, r.id, v.id, amount="1.29", observed_at=T0)  # same-timestamp conflict
    m = _make_manifest(db_session)
    actions = {row["action"] for lane in m["lanes"] for row in lane["rows"]}
    assert "mark_disputed_same_timestamp_conflict" in actions
    assert "supported_actions_only" in _blocking(db_session, m, _ctx(db_session))


# --------------------------------------------------------------------------- #
# §8 write guard: connection-scoped, allowlist, isolation, ORM
# --------------------------------------------------------------------------- #
def test_guard_forbids_fact_identity_and_delete(db_session: Session) -> None:  # §11.23/§11.24
    r, v = _fixture(db_session)
    a, _b = _dup_lane(db_session, r.id, v.id)
    with apply_tool._WriteGuard(db_session), pytest.raises(apply_tool.ApplyForbiddenWrite):
        db_session.execute(text("UPDATE price_observation SET amount = 9.99 WHERE id = :i"),
                           {"i": a.id})
    db_session.rollback()
    with apply_tool._WriteGuard(db_session), pytest.raises(apply_tool.ApplyForbiddenWrite):
        db_session.execute(text("DELETE FROM price_observation WHERE id = :i"), {"i": a.id})
    db_session.rollback()
    with apply_tool._WriteGuard(db_session), pytest.raises(apply_tool.ApplyForbiddenWrite):
        db_session.execute(text("DELETE FROM price_observation_occurrence WHERE id = 1"))
    db_session.rollback()


def test_guard_allows_whitelisted_update(db_session: Session) -> None:  # §8
    r, v = _fixture(db_session)
    a, _b = _dup_lane(db_session, r.id, v.id)
    with apply_tool._WriteGuard(db_session):
        db_session.execute(text("UPDATE price_observation SET valid_until = :t WHERE id = :i"),
                           {"t": T1, "i": a.id})
    db_session.rollback()


def test_guard_anomaly_delete_allowlist(db_session: Session) -> None:  # §8
    an = PriceAnomaly(anomaly_type="x", severity="low", status="open")
    db_session.add(an)
    db_session.flush()
    allowed = frozenset({an.id})
    guard = apply_tool._WriteGuard(db_session, allow_anomaly_delete=True,
                                   allowed_anomaly_ids=allowed)
    with guard, pytest.raises(apply_tool.ApplyForbiddenWrite):
        db_session.execute(text("DELETE FROM price_anomaly WHERE id = :i"), {"i": an.id + 999999})
    db_session.rollback()
    db_session.add(an := PriceAnomaly(anomaly_type="x", severity="low", status="open"))
    db_session.flush()
    with apply_tool._WriteGuard(db_session, allow_anomaly_delete=True,
                                allowed_anomaly_ids=frozenset({an.id})), \
            pytest.raises(apply_tool.ApplyForbiddenWrite):
        db_session.execute(text("DELETE FROM price_anomaly"))  # bulk, no WHERE
    db_session.rollback()


def test_guard_is_connection_scoped(db_session: Session) -> None:  # §8
    # A second, independent connection is NOT affected by this guard.
    other = Session(bind=engine.connect(), expire_on_commit=False)
    try:
        with apply_tool._WriteGuard(db_session):
            other.execute(text("SELECT 1"))  # no guard on `other` -> no interference
            r = Retailer(slug=f"g-{uuid.uuid4().hex[:8]}", name="G", adapter_key="test",
                         is_synthetic=True)
            other.add(r)
            other.flush()  # a write on `other` is NOT intercepted by db_session's guard
        other.rollback()
    finally:
        other.close()


# --------------------------------------------------------------------------- #
# §10 session gates
# --------------------------------------------------------------------------- #
def test_apply_rejects_dirty_session(db_session: Session) -> None:  # §10
    r, v = _fixture(db_session)
    _dup_lane(db_session, r.id, v.id)
    m = _make_manifest(db_session)
    ctx = _ctx(db_session)  # build ctx first (it queries -> autoflush); THEN dirty the session
    r.name = "mutated"  # a dirty object -> apply must reject before any SQL / autoflush
    with pytest.raises(apply_tool.ApplySessionNotClean):
        _apply(db_session, m, ctx)


def test_apply_rejects_new_session(db_session: Session) -> None:  # §10
    r, v = _fixture(db_session)
    _dup_lane(db_session, r.id, v.id)
    m = _make_manifest(db_session)
    ctx = _ctx(db_session)
    db_session.add(Retailer(slug="pending-x", name="x", adapter_key="test", is_synthetic=True))
    with pytest.raises(apply_tool.ApplySessionNotClean):
        _apply(db_session, m, ctx)
    db_session.rollback()


def test_gates_hold_under_optimize() -> None:  # §11.30
    code = (
        "from cestaplan_api.tools import apply_history_lane_remediation as a\n"
        "try:\n"
        "    a.load_manifest('/nonexistent/manifest.json'); print('NO_RAISE')\n"
        "except a.ApplyManifestInvalid as e:\n"
        "    print('RAISED', e.code)\n")
    import sys
    out = subprocess.run([sys.executable, "-O", "-c", code], capture_output=True, text=True)
    assert "RAISED manifest_unreadable" in out.stdout


def test_no_sensitive_data_in_manifest_or_report(db_session: Session) -> None:  # §11.29
    _r, m = _dup_manifest(db_session)
    rep = _verify(db_session, m, _ctx(db_session))
    assert planner.scan_sensitive(m) == [] and planner.scan_sensitive(rep) == []


# --------------------------------------------------------------------------- #
# authorization gate + §11.21/§11.22 concurrency + §6 concurrent restore
# --------------------------------------------------------------------------- #
def test_apply_blocked_without_authorization(db_session: Session) -> None:
    _r, m = _dup_manifest(db_session)
    with pytest.raises(apply_tool.ApplyNotAuthorized):
        apply_tool._apply_guarded(db_session, m, _ctx(db_session))
    with pytest.raises(apply_tool.ApplyNotAuthorized):
        apply_tool._apply_guarded(db_session, m, _ctx(db_session), authorized=True,
                                  confirmations=("x",))


def _isession() -> Session:
    return Session(bind=engine.connect(), expire_on_commit=False)


def _seed_committed():
    slug = f"apc-{uuid.uuid4().hex[:10]}"
    s = _isession()
    try:
        r = Retailer(slug=slug, name="ApplyConc", adapter_key="test", is_synthetic=True)
        s.add(r)
        s.flush()
        ext = ExternalProduct(retailer_id=r.id, external_id="APC-1")
        s.add(ext)
        s.flush()
        pv = ProductVariant(retailer_id=r.id, external_product_id=ext.id, display_name="V",
                            product_id=None)
        s.add(pv)
        s.flush()
        for _ in range(2):
            s.add(PriceObservation(
                retailer_id=r.id, product_variant_id=pv.id, price_scope="national",
                price_type="regular", amount=Decimal("1.19"), currency="EUR",
                requires_loyalty=False,
                observed_at=T0, imported_at=T0, valid_from=T0, confidence_score=Decimal("1.0"),
                staging_only=True))
        s.commit()
        return slug, r.id
    finally:
        s.close()


def _manifest_committed(slug: str) -> dict:
    s = _isession()
    try:
        s.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
        m = json.loads(json.dumps(planner._dry_run_in_snapshot(s, slug)["manifest"], default=str))
        s.rollback()
        return m
    finally:
        s.close()


def _cleanup(rid: int) -> None:
    c = _isession()
    try:
        oids = c.execute(select(PriceObservation.id).where(
            PriceObservation.retailer_id == rid)).scalars().all()
        rids = c.execute(select(HistoryRemediationChange.remediation_run_id).where(
            HistoryRemediationChange.price_observation_id.in_(oids))
            ).scalars().all() if oids else []
        if oids:
            c.execute(delete(HistoryRemediationChange).where(
                HistoryRemediationChange.price_observation_id.in_(oids)))
            c.execute(delete(PriceAnomaly).where(PriceAnomaly.price_observation_id.in_(oids)))
        if rids:
            c.execute(delete(HistoryRemediationPlanConsumption).where(
                HistoryRemediationPlanConsumption.first_run_id.in_(set(rids))))
            c.execute(delete(HistoryRemediationRun).where(HistoryRemediationRun.id.in_(set(rids))))
        c.execute(delete(PriceObservation).where(PriceObservation.retailer_id == rid))
        c.execute(delete(ProductVariant).where(ProductVariant.retailer_id == rid))
        c.execute(delete(ExternalProduct).where(ExternalProduct.retailer_id == rid))
        c.execute(delete(Retailer).where(Retailer.id == rid))
        c.commit()
    finally:
        c.close()


@pytest.fixture()
def committed_dup_lane():
    slug, rid = _seed_committed()
    try:
        yield slug, rid
    finally:
        _cleanup(rid)


def test_two_concurrent_applies_only_one_wins(committed_dup_lane) -> None:  # §11.21
    slug, _rid = committed_dup_lane
    m = _manifest_committed(slug)
    results: list[str] = []
    barrier = threading.Barrier(2)

    probe = _isession()
    ctx = _ctx(probe, m=m)  # build ctx off a throwaway session; execute_apply owns its own session
    probe.close()

    def run():
        try:
            barrier.wait(timeout=30)
            res = apply_tool.execute_apply(m, ctx, authorized=True, confirmations=CONFIRM)
            results.append(res["status"])
        except Exception as exc:
            results.append(type(exc).__name__)

    threads = [threading.Thread(target=run) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert results.count("applied") == 1


def test_apply_then_restore_committed(committed_dup_lane) -> None:  # §11.22 / §6 / §1v4
    slug, rid = committed_dup_lane
    m = _manifest_committed(slug)
    probe = _isession()
    apply_ctx = _ctx(probe, m=m)
    probe.close()
    # execute_apply owns the session AND commits; the caller never manages the transaction.
    res = apply_tool.execute_apply(m, apply_ctx, authorized=True, confirmations=CONFIRM)
    assert res["status"] == "applied"
    c = _isession()
    try:
        rows = c.execute(select(PriceObservation).where(
            PriceObservation.retailer_id == rid)).scalars().all()
        assert len(rows) == 2 and sum(1 for x in rows if x.rolled_back_at is not None) == 1
    finally:
        c.close()
    probe = _isession()
    restore_ctx = _ctx(probe, m=m)
    probe.close()
    rest = apply_tool.execute_restore(res["run_public_id"], restore_ctx, authorized=True,
                                      confirmations=RESTORE_CONFIRM)
    assert rest["status"] == "restored"


# =========================================================================== #
# v3 hardening — §1 irreversible idempotency, §2 retries, §3 unexpected error,
# §4 restore revalidation, §5 anomaly verify, §6 session gate, §7 backup
# versioning, §9 deterministic action id.
# =========================================================================== #

# --------------------------------------------------------------------------- #
# §1 A plan_hash applied once can NEVER apply again, even after a restore.
# --------------------------------------------------------------------------- #
def test_consumption_row_is_durable_and_survives_restore(db_session: Session) -> None:  # §1
    from cestaplan_api.models import HistoryRemediationPlanConsumption
    r, v = _fixture(db_session)
    _dup_lane(db_session, r.id, v.id)
    m = _make_manifest(db_session)
    res = _apply(db_session, m, _ctx(db_session))
    cons = db_session.execute(select(HistoryRemediationPlanConsumption).where(
        HistoryRemediationPlanConsumption.plan_hash == m["plan_hash"])).scalar_one()
    assert cons.execution_hash is not None
    _restore(db_session, res["run_public_id"], _ctx(db_session))
    # The immutable consumption record is NOT removed by a restore.
    still = db_session.execute(select(HistoryRemediationPlanConsumption).where(
        HistoryRemediationPlanConsumption.plan_hash == m["plan_hash"])).scalar_one()
    assert still.id == cons.id


def test_reapply_after_restore_requires_regeneration(db_session: Session) -> None:  # §1
    r, v = _fixture(db_session)
    _dup_lane(db_session, r.id, v.id)
    m = _make_manifest(db_session)
    res = _apply(db_session, m, _ctx(db_session))
    _restore(db_session, res["run_public_id"], _ctx(db_session))
    # Same plan_hash, over the restored state -> can never apply again.
    again = _apply(db_session, m, _ctx(db_session))
    assert again["status"] == "plan_requires_regeneration"


def test_new_plan_over_restored_state_applies(db_session: Session) -> None:  # §1
    r, v = _fixture(db_session)
    _dup_lane(db_session, r.id, v.id)
    m = _make_manifest(db_session)
    res = _apply(db_session, m, _ctx(db_session))
    _restore(db_session, res["run_public_id"], _ctx(db_session))
    _dup_lane(db_session, r.id, v.id)  # genuinely new state
    m2 = _make_manifest(db_session)
    assert m2["plan_hash"] != m["plan_hash"]  # a truly NEW plan over the restored state
    assert _apply(db_session, m2, _ctx(db_session))["status"] == "applied"


# --------------------------------------------------------------------------- #
# §2 retries linked by an explicit supersedes_run_id
# --------------------------------------------------------------------------- #
def test_retry_links_supersedes_run(committed_dup_lane, monkeypatch) -> None:  # §2
    slug, _rid = committed_dup_lane
    m = _manifest_committed(slug)
    monkeypatch.setattr(apply_tool, "_apply_row",
                        lambda *a, **k: _raise(apply_tool.ApplyPlanDrift("injected")))
    s1 = _isession()
    try:
        with pytest.raises(apply_tool.ApplyPlanDrift) as ei:
            apply_tool._apply_guarded(s1, m, _ctx(s1, m=m), authorized=True, confirmations=CONFIRM)
        fid = ei.value.failed_run_id
    finally:
        s1.rollback()
        s1.close()
    assert fid is not None  # the failed run's public_id is registered for the caller
    monkeypatch.undo()
    s2 = _isession()
    try:
        res = apply_tool._apply_guarded(s2, m, _ctx(s2, m=m), authorized=True,
                                        confirmations=CONFIRM,
                                        previous_failed_run_id=fid)
        s2.commit()
        assert res["status"] == "applied"
    finally:
        s2.close()
    try:
        chk = _isession()
        try:
            applied = chk.execute(select(HistoryRemediationRun).where(
                HistoryRemediationRun.plan_hash == m["plan_hash"],
                HistoryRemediationRun.status == "applied")).scalar_one()
            failed = chk.execute(select(HistoryRemediationRun).where(
                HistoryRemediationRun.plan_hash == m["plan_hash"],
                HistoryRemediationRun.status == "failed")).scalar_one()
            assert applied.supersedes_run_id == failed.id
        finally:
            chk.close()
    finally:
        _delete_runs(m["plan_hash"])


def _insert_run(db, *, plan_hash, status, supersedes=None):
    run = HistoryRemediationRun(
        plan_hash=plan_hash, manifest_schema_version=4, planner_tool_version="t",
        planner_source_hash="s", writer_contract_version="w", main_commit_sha="c",
        alembic_revision="a", execution_mode="apply", status=status, supersedes_run_id=supersedes)
    db.add(run)
    db.flush()
    return run


def test_retry_blocks_nonexistent_previous(db_session: Session) -> None:  # §2
    with pytest.raises(apply_tool.ApplyManifestInvalid) as ei:
        apply_tool._validate_retry(db_session, "ph", str(uuid.uuid4()))
    assert ei.value.code == "retry_previous_run_not_found"


def test_retry_blocks_plan_hash_mismatch(db_session: Session) -> None:  # §2
    prev = _insert_run(db_session, plan_hash="A", status="failed")
    with pytest.raises(apply_tool.ApplyManifestInvalid) as ei:
        apply_tool._validate_retry(db_session, "B", str(prev.public_id))
    assert ei.value.code == "retry_plan_hash_mismatch"


def test_retry_blocks_non_failed_previous(db_session: Session) -> None:  # §2
    prev = _insert_run(db_session, plan_hash="A", status="applied")
    with pytest.raises(apply_tool.ApplyManifestInvalid) as ei:
        apply_tool._validate_retry(db_session, "A", str(prev.public_id))
    assert ei.value.code == "retry_previous_not_failed"


def test_retry_blocks_already_superseded(db_session: Session) -> None:  # §2
    prev = _insert_run(db_session, plan_hash="A", status="failed")
    _insert_run(db_session, plan_hash="A", status="failed", supersedes=prev.id)
    with pytest.raises(apply_tool.ApplyManifestInvalid) as ei:
        apply_tool._validate_retry(db_session, "A", str(prev.public_id))
    assert ei.value.code == "retry_previous_already_superseded"


# The circular-chain guard in _validate_retry is defensive: the partial-unique index on
# supersedes_run_id forces in-degree <= 1, so every node in a cycle is itself superseded and the
# already-superseded gate fires first. A valid cyclic state is therefore unconstructable through
# the public path; the already-superseded case above is the reachable prevention.


# --------------------------------------------------------------------------- #
# §3 an unexpected (non-ApplyError) exception -> rollback + sanitized durable run
# --------------------------------------------------------------------------- #
def test_unexpected_error_rolls_back_and_sanitizes(committed_dup_lane, monkeypatch) -> None:  # §3
    slug, rid = committed_dup_lane
    m = _manifest_committed(slug)
    monkeypatch.setattr(apply_tool, "_apply_row",
                        lambda *a, **k: _raise(RuntimeError("SECRET amount=42 SELECT * FROM x")))
    s = _isession()
    try:
        with pytest.raises(RuntimeError):
            apply_tool._apply_guarded(s, m, _ctx(s, m=m), authorized=True, confirmations=CONFIRM)
    finally:
        s.rollback()
        s.close()
    monkeypatch.undo()
    try:
        chk = _isession()
        try:
            failed = chk.execute(select(HistoryRemediationRun).where(
                HistoryRemediationRun.plan_hash == m["plan_hash"],
                HistoryRemediationRun.status == "failed")).scalar_one()
            assert failed.error_code == "unexpected_apply_error"  # sanitized code, never the msg
            assert "SECRET" not in (failed.error_code or "") and "SELECT" not in (
                failed.error_code or "")
            obs = chk.execute(select(PriceObservation).where(
                PriceObservation.retailer_id == rid)).scalars().all()
            assert all(o.rolled_back_at is None for o in obs)  # zero business changes
        finally:
            chk.close()
    finally:
        _delete_runs(m["plan_hash"])


# --------------------------------------------------------------------------- #
# §4 restore revalidates ALL gates against the stored post-apply evidence
# --------------------------------------------------------------------------- #
def _apply_dup(db):
    r, v = _fixture(db)
    a, b = _dup_lane(db, r.id, v.id)
    m = _make_manifest(db)
    res = _apply(db, m, _ctx(db))
    run = db.execute(select(HistoryRemediationRun).where(
        HistoryRemediationRun.plan_hash == m["plan_hash"])).scalar_one()
    return r, a, b, m, res, run


def test_restore_blocks_on_added_occurrence(db_session: Session) -> None:  # §4
    _r, a, _b, _m, res, _run = _apply_dup(db_session)
    db_session.add(PriceObservationOccurrence(price_observation_id=a.id, provider_code="late",
                                              imported_at=T0))
    db_session.flush()
    with pytest.raises(apply_tool.ApplyRestoreDrift) as ei:
        _restore(db_session, res["run_public_id"], _ctx(db_session))
    assert ei.value.code == "occurrences_changed_after_apply"


def test_restore_blocks_on_added_promotion_rule(db_session: Session) -> None:  # §4
    _r, a, _b, _m, res, _run = _apply_dup(db_session)
    db_session.add(PromotionRule(price_observation_id=a.id, type="percentage"))
    db_session.flush()
    with pytest.raises(apply_tool.ApplyRestoreDrift) as ei:
        _restore(db_session, res["run_public_id"], _ctx(db_session))
    assert ei.value.code == "supported_fk_changed_after_apply"


def test_restore_blocks_on_new_unknown_fk(db_session: Session) -> None:  # §4
    _r, a, _b, _m, res, _run = _apply_dup(db_session)
    db_session.execute(text("CREATE TABLE ap_restore_unknown (id bigint PRIMARY KEY, "
                            "obs_id bigint REFERENCES price_observation(id))"))
    db_session.execute(text("INSERT INTO ap_restore_unknown (id, obs_id) VALUES (1, :o)"),
                       {"o": a.id})
    db_session.flush()
    with pytest.raises(apply_tool.ApplyRestoreDrift) as ei:
        _restore(db_session, res["run_public_id"], _ctx(db_session))
    assert ei.value.code in ("unknown_fk_after_apply", "fk_schema_changed_after_apply")


def test_restore_blocks_on_row_changed_after_apply(db_session: Session) -> None:  # §4
    _r, _a, _b, _m, res, run = _apply_dup(db_session)
    ch = db_session.execute(select(HistoryRemediationChange).where(
        HistoryRemediationChange.remediation_run_id == run.id,
        HistoryRemediationChange.action_type.in_(list(apply_tool._ACTION_WRITES)))).scalars().first()
    assert ch is not None
    obs = db_session.get(PriceObservation, ch.price_observation_id)
    assert obs is not None
    obs.valid_until = T2  # drift the applied row out of its post-apply state
    db_session.flush()
    with pytest.raises(apply_tool.ApplyRestoreDrift) as ei:
        _restore(db_session, res["run_public_id"], _ctx(db_session))
    assert ei.value.code == "row_changed_after_apply"


def test_restore_blocks_on_api_worker_misaligned(db_session: Session) -> None:  # §4
    _r, _a, _b, _m, res, _run = _apply_dup(db_session)
    with pytest.raises(apply_tool.ApplyEnvironmentUnsafe) as ei:
        _restore(db_session, res["run_public_id"], _ctx(db_session, deployed_worker_sha="f" * 40))
    assert ei.value.code == "restore_gates_blocking"


def test_restore_blocks_on_commit_differs(db_session: Session) -> None:  # §4
    _r, _a, _b, _m, res, _run = _apply_dup(db_session)
    with pytest.raises(apply_tool.ApplyEnvironmentUnsafe) as ei:
        _restore(db_session, res["run_public_id"], _ctx(db_session, app_commit_sha="b" * 40,
                 deployed_api_sha="b" * 40, deployed_worker_sha="b" * 40))
    assert ei.value.code == "restore_gates_blocking"


# --------------------------------------------------------------------------- #
# §5 anomaly is verified before deletion; only the exact single-id ORM DELETE passes
# --------------------------------------------------------------------------- #
def _apply_anomaly(db):
    m, _r = _manifest_with_anomaly(db)
    res = _apply(db, m, _ctx(db))
    run = db.execute(select(HistoryRemediationRun).where(
        HistoryRemediationRun.plan_hash == m["plan_hash"])).scalar_one()
    ch = db.execute(select(HistoryRemediationChange).where(
        HistoryRemediationChange.remediation_run_id == run.id,
        HistoryRemediationChange.created_anomaly_live_id.is_not(None))).scalar_one()
    return m, res, ch


def test_anomaly_lifecycle_end_to_end(db_session: Session) -> None:  # §5 happy path
    # The exact single-object ORM delete of a verified anomaly is exercised by
    # test_anomaly_created_then_removed_only_by_restore; here we assert the run recorded it.
    _m, _res, ch = _apply_anomaly(db_session)
    assert ch.created_anomaly_live_id is not None and ch.created_anomaly_hash is not None


# _verify_anomaly_before_delete is unit-tested directly: the price_anomaly -> price_observation FK
# is domain_supported, so the supported-FK evidence gate already trips on any anomaly tampering
# BEFORE _restore_locked runs. These prove the per-anomaly verification itself fails closed.
def test_verify_anomaly_blocks_missing(db_session: Session) -> None:  # §5
    fake = SimpleNamespace(created_anomaly_live_id=10 ** 12, created_anomaly_hash="x",
                           price_observation_id=1)
    with pytest.raises(apply_tool.ApplyRestoreDrift) as ei:
        apply_tool._verify_anomaly_before_delete(db_session, fake)  # type: ignore[arg-type]
    assert ei.value.code == "anomaly_missing"


def test_verify_anomaly_blocks_changed(db_session: Session) -> None:  # §5
    an = PriceAnomaly(anomaly_type="x", severity="high", status="open")
    db_session.add(an)
    db_session.flush()
    good_hash = apply_tool._anomaly_hash(an)
    an.severity = "low"  # tamper AFTER capturing the recorded hash
    db_session.flush()
    fake = SimpleNamespace(created_anomaly_live_id=an.id, created_anomaly_hash=good_hash,
                           price_observation_id=an.price_observation_id)
    with pytest.raises(apply_tool.ApplyRestoreDrift) as ei:
        apply_tool._verify_anomaly_before_delete(db_session, fake)  # type: ignore[arg-type]
    assert ei.value.code == "anomaly_changed"


def test_verify_anomaly_accepts_exact_match(db_session: Session) -> None:  # §5
    an = PriceAnomaly(anomaly_type="x", severity="high", status="open")
    db_session.add(an)
    db_session.flush()
    fake = SimpleNamespace(created_anomaly_live_id=an.id,
                           created_anomaly_hash=apply_tool._anomaly_hash(an),
                           price_observation_id=an.price_observation_id)
    assert apply_tool._verify_anomaly_before_delete(db_session, fake) is an  # type: ignore[arg-type]


def test_guard_rejects_non_single_id_anomaly_delete(db_session: Session) -> None:  # §5
    an = PriceAnomaly(anomaly_type="x", severity="low", status="open")
    db_session.add(an)
    db_session.flush()
    allowed = frozenset({an.id})
    for sql in (f"DELETE FROM price_anomaly WHERE id = {an.id} OR id = 1",
                f"DELETE FROM price_anomaly WHERE id IN ({an.id})",
                "DELETE FROM price_anomaly WHERE id = (SELECT id FROM price_anomaly LIMIT 1)",
                f"DELETE FROM price_anomaly WHERE id = {an.id} AND severity = 'low'"):
        with apply_tool._WriteGuard(db_session, allow_anomaly_delete=True,
                                    allowed_anomaly_ids=allowed), \
                pytest.raises(apply_tool.ApplyForbiddenWrite):
            db_session.execute(text(sql))
        db_session.rollback()


# --------------------------------------------------------------------------- #
# §6 public apply/restore require a session THIS call owns (a virgin transaction)
# --------------------------------------------------------------------------- #
def test_virgin_gate_rejects_prior_query() -> None:  # §6
    s = _isession()
    try:
        s.execute(text("SELECT 1"))  # a prior statement opened the transaction
        with pytest.raises(apply_tool.ApplySessionNotClean):
            apply_tool._require_virgin_session(s)
    finally:
        s.rollback()
        s.close()


def test_virgin_gate_rejects_explicit_begin() -> None:  # §6
    s = _isession()
    try:
        s.begin()
        with pytest.raises(apply_tool.ApplySessionNotClean):
            apply_tool._require_virgin_session(s)
    finally:
        s.rollback()
        s.close()


def test_virgin_gate_rejects_begin_nested() -> None:  # §6
    s = _isession()
    try:
        s.execute(text("SELECT 1"))
        s.begin_nested()
        with pytest.raises(apply_tool.ApplySessionNotClean):
            apply_tool._require_virgin_session(s)
    finally:
        s.rollback()
        s.close()


def test_virgin_gate_accepts_fresh_and_after_rollback() -> None:  # §6
    s = _isession()
    try:
        apply_tool._require_virgin_session(s)  # brand-new -> ok
        s.execute(text("SELECT 1"))
        s.rollback()
        apply_tool._require_virgin_session(s)  # rolled back to virgin -> ok again
    finally:
        s.close()


# --------------------------------------------------------------------------- #
# §7 backup version compatibility is actually checked
# --------------------------------------------------------------------------- #
def test_backup_incompatible_version_blocks() -> None:  # §7
    now = datetime.now(UTC)
    be = apply_tool.BackupEvidence(path=_BACKUP["path"], expected_sha256=_BACKUP["sha256"],
                                   created_at=now, expected_postgres_version="99")
    ok, ev = be.verify(now, server_version="12.22")
    assert ok is False and ev["compatibility_ok"] is False


def test_backup_pg_restore_absent_blocks(monkeypatch) -> None:  # §7
    real = apply_tool.subprocess.run

    def fake(cmd, *a, **k):
        if isinstance(cmd, (list, tuple)) and cmd and cmd[0] == "pg_restore":
            raise FileNotFoundError("pg_restore")
        return real(cmd, *a, **k)

    monkeypatch.setattr(apply_tool.subprocess, "run", fake)
    now = datetime.now(UTC)
    be = _backup_evidence(now)
    ok, ev = be.verify(now, server_version=_BACKUP["pg_major"])
    assert ok is False and ev["pg_restore_list_verified"] is False


def test_backup_corrupt_dump_blocks() -> None:  # §7
    fd, p = tempfile.mkstemp(suffix=".dump")
    os.write(fd, b"this is not a valid custom-format dump")
    os.close(fd)
    os.chmod(p, 0o600)
    now = datetime.now(UTC)
    be = apply_tool.BackupEvidence(
        path=p, expected_sha256=hashlib.sha256(Path(p).read_bytes()).hexdigest(),
        created_at=now, expected_postgres_version=_BACKUP["pg_major"])
    ok, ev = be.verify(now, server_version="12.22")
    os.unlink(p)
    assert ok is False and ev["pg_restore_list_verified"] is False


def test_backup_unsanitized_reference_blocks() -> None:  # §7
    now = datetime.now(UTC)
    be = apply_tool.BackupEvidence(
        path=_BACKUP["path"], expected_sha256=_BACKUP["sha256"], created_at=now,
        expected_postgres_version=_BACKUP["pg_major"],
        storage_reference="s3://user:secret@host/backup.dump")
    ok, ev = be.verify(now, server_version=_BACKUP["pg_major"])
    assert ok is False and ev["reference_sanitized"] is False


def test_backup_full_evidence_persisted(db_session: Session) -> None:  # §7
    _r, _a, _b, _m, _res, run = _apply_dup(db_session)
    assert run.backup_pg_restore_version == _BACKUP["pg_major"]
    assert run.backup_database_version == _BACKUP["pg_major"]
    assert run.backup_dump_database_version == _BACKUP["pg_major"]
    assert run.backup_permissions_verified is True
    assert run.backup_evidence_hash is not None


# --------------------------------------------------------------------------- #
# §9 deterministic_action_id derives ONLY from the sealed manifest
# --------------------------------------------------------------------------- #
def test_deterministic_id_derives_from_sealed_manifest(db_session: Session) -> None:  # §9
    m, _res, _ch = _apply_anomaly(db_session)
    run = db_session.execute(select(HistoryRemediationRun).where(
        HistoryRemediationRun.plan_hash == m["plan_hash"])).scalar_one()
    lane = next(x for x in m["lanes"] if not x["excluded"])
    expected = {
        apply_tool._det_action_id(
            m["plan_hash"], lane["lane_fingerprint"], row["integrity"]["full_row_hash"],
            row["action"], apply_tool._sealed_side_effect_ref(
                lane, row["integrity"]["full_row_hash"]))
        for row in lane["rows"]}
    got = {ch.deterministic_action_id for ch in db_session.execute(select(
        HistoryRemediationChange).where(
        HistoryRemediationChange.remediation_run_id == run.id)).scalars()}
    assert got == expected  # computable from the sealed manifest alone, before any INSERT


def test_deterministic_id_ignores_db_ids() -> None:  # §9
    ref = apply_tool._det_action_id("ph", "lf", "r" * 64, "keep", "se")
    assert ref == apply_tool._det_action_id("ph", "lf", "r" * 64, "keep", "se")


def test_deterministic_id_changes_with_side_effect() -> None:  # §9
    lane_hi = {"proposed_side_effects": [{"type": "create_price_anomaly",
               "target_observation_ref": "rh", "anomaly_type": "x", "severity": "high"}]}
    lane_lo = copy.deepcopy(lane_hi)
    lane_lo["proposed_side_effects"][0]["severity"] = "low"
    assert (apply_tool._sealed_side_effect_ref(lane_hi, "rh")
            != apply_tool._sealed_side_effect_ref(lane_lo, "rh"))


def test_deterministic_id_no_collision_over_196() -> None:  # §9
    ids = {apply_tool._det_action_id("ph", f"lane-{i}", f"{'a' * 62}{i:02d}",
                                     "logical_rollback_exact_duplicate", "")
           for i in range(196)}
    assert len(ids) == 196


# =========================================================================== #
# v4 FINAL — §1 transaction-owning public API + durable commit, §2 all-rows
# restore gate, §3 restore provenance bound to the run, §4 sanitized storage ref.
# =========================================================================== #
_COMMIT2 = "1" * 40
_SRC2, _API2, _WRK2, _DOC2 = "1" * 64, "2" * 64, "3" * 64, "4" * 64


def _run_row(plan_hash: str, *, status: str) -> dict | None:
    """Read a run via an INDEPENDENT connection, returning plain fields (session then closed)."""
    s = _isession()
    try:
        run = s.execute(select(HistoryRemediationRun).where(
            HistoryRemediationRun.plan_hash == plan_hash,
            HistoryRemediationRun.status == status)).scalar_one_or_none()
        if run is None:
            return None
        return {"id": run.id, "public_id": str(run.public_id), "status": run.status,
                "restore_status": run.restore_status, "error_code": run.error_code}
    finally:
        s.close()


def _rolled_back_count(rid: int) -> int:
    s = _isession()
    try:
        rows = s.execute(select(PriceObservation).where(
            PriceObservation.retailer_id == rid)).scalars().all()
        return sum(1 for x in rows if x.rolled_back_at is not None)
    finally:
        s.close()


# --------------------------------------------------------------------------- #
# §1 execute_apply / execute_restore own the session AND commit before success
# --------------------------------------------------------------------------- #
def test_execute_apply_commits_and_is_visible_to_another_connection(committed_dup_lane) -> None:
    slug, rid = committed_dup_lane
    m = _manifest_committed(slug)
    probe = _isession()
    ctx = _ctx(probe, m=m)
    probe.close()
    try:
        res = apply_tool.execute_apply(m, ctx, authorized=True, confirmations=CONFIRM)
        assert res["status"] == "applied"
        # A SECOND independent connection must see the durable results at once (no caller commit).
        chk = _isession()
        try:
            run = chk.execute(select(HistoryRemediationRun).where(
                HistoryRemediationRun.plan_hash == m["plan_hash"])).scalar_one()
            assert run.status == "applied"
            changes = chk.execute(select(HistoryRemediationChange).where(
                HistoryRemediationChange.remediation_run_id == run.id)).scalars().all()
            assert len(changes) == 2
            cons = chk.execute(select(HistoryRemediationPlanConsumption).where(
                HistoryRemediationPlanConsumption.plan_hash == m["plan_hash"])).scalar_one()
            assert cons is not None
        finally:
            chk.close()
        assert _rolled_back_count(rid) == 1  # temporal write is durable too
    finally:
        _delete_runs(m["plan_hash"])


def test_execute_restore_commits_and_is_visible_to_another_connection(committed_dup_lane) -> None:
    slug, rid = committed_dup_lane
    m = _manifest_committed(slug)
    probe = _isession()
    actx = _ctx(probe, m=m)
    probe.close()
    try:
        res = apply_tool.execute_apply(m, actx, authorized=True, confirmations=CONFIRM)
        probe = _isession()
        rctx = _ctx(probe, m=m)
        probe.close()
        rest = apply_tool.execute_restore(res["run_public_id"], rctx, authorized=True,
                                          confirmations=RESTORE_CONFIRM)
        assert rest["status"] == "restored"
        chk = _isession()
        try:
            run = chk.execute(select(HistoryRemediationRun).where(
                HistoryRemediationRun.plan_hash == m["plan_hash"])).scalar_one()
            assert run.status == "rolled_back" and run.restore_status == "restored"
            # plan consumption is STILL present after restore (§1)
            cons = chk.execute(select(HistoryRemediationPlanConsumption).where(
                HistoryRemediationPlanConsumption.plan_hash == m["plan_hash"])).scalar_one()
            assert cons is not None
        finally:
            chk.close()
        assert _rolled_back_count(rid) == 0  # rows restored, durably
    finally:
        _delete_runs(m["plan_hash"])


def test_execute_apply_failure_before_commit_leaves_zero_changes(committed_dup_lane,
                                                                 monkeypatch) -> None:
    slug, rid = committed_dup_lane
    m = _manifest_committed(slug)
    probe = _isession()
    ctx = _ctx(probe, m=m)
    probe.close()
    monkeypatch.setattr(apply_tool, "_apply_row",
                        lambda *a, **k: _raise(apply_tool.ApplyPlanDrift("injected")))
    try:
        with pytest.raises(apply_tool.ApplyPlanDrift):
            apply_tool.execute_apply(m, ctx, authorized=True, confirmations=CONFIRM)
        monkeypatch.undo()
        assert _run_row(m["plan_hash"], status="applied") is None  # never committed applied
        assert _run_row(m["plan_hash"], status="failed") is not None  # durable failed run
        assert _rolled_back_count(rid) == 0  # zero business changes
    finally:
        _delete_runs(m["plan_hash"])


def test_execute_apply_commit_failure_does_not_return_applied(committed_dup_lane) -> None:
    slug, rid = committed_dup_lane
    m = _manifest_committed(slug)
    probe = _isession()
    ctx = _ctx(probe, m=m)
    probe.close()

    def failing_factory():
        s = apply_tool.SessionLocal()
        s.commit = lambda: _raise(RuntimeError("commit boom"))  # type: ignore[method-assign]
        return s

    try:
        with pytest.raises(RuntimeError):
            apply_tool.execute_apply(m, ctx, session_factory=failing_factory,
                                     authorized=True, confirmations=CONFIRM)
        assert _run_row(m["plan_hash"], status="applied") is None  # commit failed -> not applied
        failed = _run_row(m["plan_hash"], status="failed")
        assert failed is not None and failed["error_code"] == "apply_commit_failed"
        assert _rolled_back_count(rid) == 0  # rolled back -> zero business changes
    finally:
        _delete_runs(m["plan_hash"])


def test_execute_apply_reapply_after_session_close_is_already_applied(committed_dup_lane) -> None:
    slug, _rid = committed_dup_lane
    m = _manifest_committed(slug)
    probe = _isession()
    ctx = _ctx(probe, m=m)
    probe.close()
    try:
        assert apply_tool.execute_apply(
            m, ctx, authorized=True, confirmations=CONFIRM)["status"] == "applied"
        probe = _isession()
        ctx2 = _ctx(probe, m=m)
        probe.close()
        # session fully closed between calls; the durable consumption record still blocks re-apply.
        assert apply_tool.execute_apply(
            m, ctx2, authorized=True, confirmations=CONFIRM)["status"] == "already_applied"
    finally:
        _delete_runs(m["plan_hash"])


def test_execute_apply_restore_reapply_requires_regeneration(committed_dup_lane) -> None:
    slug, _rid = committed_dup_lane
    m = _manifest_committed(slug)
    probe = _isession()
    actx = _ctx(probe, m=m)
    probe.close()
    try:
        res = apply_tool.execute_apply(m, actx, authorized=True, confirmations=CONFIRM)
        probe = _isession()
        rctx = _ctx(probe, m=m)
        probe.close()
        apply_tool.execute_restore(res["run_public_id"], rctx, authorized=True,
                                   confirmations=RESTORE_CONFIRM)
        probe = _isession()
        actx2 = _ctx(probe, m=m)
        probe.close()
        assert apply_tool.execute_apply(
            m, actx2, authorized=True,
            confirmations=CONFIRM)["status"] == "plan_requires_regeneration"
    finally:
        _delete_runs(m["plan_hash"])


# --------------------------------------------------------------------------- #
# §2 restore validates EVERY change (not just writes) before touching a row
# --------------------------------------------------------------------------- #
def _keep_change(db, run):
    return db.execute(select(HistoryRemediationChange).where(
        HistoryRemediationChange.remediation_run_id == run.id,
        HistoryRemediationChange.action_type == "keep")).scalars().first()


def test_restore_blocks_on_keep_row_drift(db_session: Session) -> None:  # §2v4
    _r, _a, _b, _m, res, run = _apply_dup(db_session)
    ch = _keep_change(db_session, run)
    assert ch is not None  # a dup lane keeps one canonical row
    obs = db_session.get(PriceObservation, ch.price_observation_id)
    assert obs is not None
    obs.valid_until = T2  # a NON-write ("keep") row drifted -> must still block restore
    db_session.flush()
    with pytest.raises(apply_tool.ApplyRestoreDrift) as ei:
        _restore(db_session, res["run_public_id"], _ctx(db_session))
    assert ei.value.code == "row_changed_after_apply"


def test_restore_blocks_on_deleted_observation(db_session: Session) -> None:  # §2v4
    _r, a, _b, _m, res, _run = _apply_dup(db_session)
    db_session.execute(text("DELETE FROM price_observation WHERE id = :i"), {"i": a.id})
    db_session.flush()
    with pytest.raises(apply_tool.ApplyRestoreDrift) as ei:
        _restore(db_session, res["run_public_id"], _ctx(db_session))
    assert ei.value.code == "observation_missing_or_duplicated"


def test_restore_blocks_on_null_actual_after_hash(db_session: Session) -> None:  # §2v4
    # v5: a persisted-column tamper is caught by the evidence seal BEFORE the all-rows gate.
    _r, _a, _b, _m, res, run = _apply_dup(db_session)
    db_session.execute(text(
        "UPDATE history_remediation_change SET actual_after_hash = NULL "
        "WHERE remediation_run_id = :r"), {"r": run.id})
    db_session.flush()
    db_session.expire_all()
    with pytest.raises(apply_tool.ApplyRestoreDrift) as ei:
        _restore(db_session, res["run_public_id"], _ctx(db_session))
    assert ei.value.code == "apply_evidence_hash_mismatch"


def test_restore_blocks_on_altered_deterministic_id(db_session: Session) -> None:  # §2v4
    _r, _a, _b, _m, res, run = _apply_dup(db_session)
    ch = db_session.execute(select(HistoryRemediationChange).where(
        HistoryRemediationChange.remediation_run_id == run.id)).scalars().first()
    assert ch is not None
    ch.deterministic_action_id = "f" * 64  # tampered audit -> seal recompute mismatch
    db_session.flush()
    with pytest.raises(apply_tool.ApplyRestoreDrift) as ei:
        _restore(db_session, res["run_public_id"], _ctx(db_session))
    assert ei.value.code == "apply_evidence_hash_mismatch"


def _ns_change(**over):
    base = {"price_observation_id": 1, "action_type": "keep", "deterministic_action_id": "d" * 64,
            "actual_after_hash": "h", "actual_after_state": {"x": 1}, "original_hash": "r" * 64,
            "lane_fingerprint": "lf", "created_anomaly_live_id": None}
    base.update(over)
    return SimpleNamespace(**base)


def test_all_rows_gate_blocks_duplicate_change(db_session: Session) -> None:  # §2v4
    # Two changes for the same (observation, action) — a real duplicate always carries a distinct
    # deterministic_action_id (unique index), so the structural pass reports it as a duplicate.
    r, v = _fixture(db_session)
    a = _obs(db_session, r.id, v.id, amount="1.19", observed_at=T0)
    c1 = _ns_change(price_observation_id=a.id, deterministic_action_id="1" * 64)
    c2 = _ns_change(price_observation_id=a.id, deterministic_action_id="2" * 64)
    with pytest.raises(apply_tool.ApplyRestoreDrift) as ei:
        apply_tool._validate_all_changes(
            db_session, SimpleNamespace(plan_hash="p"), [c1, c2],  # type: ignore[arg-type,list-item]
            {a.id: a}, {})
    assert ei.value.code == "duplicate_change_for_observation"


def test_all_rows_gate_blocks_null_actual_after(db_session: Session) -> None:  # §2v4
    r, v = _fixture(db_session)
    a = _obs(db_session, r.id, v.id, amount="1.19", observed_at=T0)
    ch = _ns_change(price_observation_id=a.id, actual_after_hash=None)
    with pytest.raises(apply_tool.ApplyRestoreDrift) as ei:
        apply_tool._validate_all_changes(
            db_session, SimpleNamespace(plan_hash="p"), [ch],  # type: ignore[arg-type,list-item]
            {a.id: a}, {})
    assert ei.value.code == "actual_after_missing"


def test_all_rows_gate_blocks_invalid_deterministic_id(db_session: Session) -> None:  # §2v4
    r, v = _fixture(db_session)
    a = _obs(db_session, r.id, v.id, amount="1.19", observed_at=T0)
    ch = _ns_change(price_observation_id=a.id,
                    actual_after_hash=apply_tool._thash(apply_tool._temporal_of(a)),
                    actual_after_state=apply_tool._json(apply_tool._temporal_of(a)))
    with pytest.raises(apply_tool.ApplyRestoreDrift) as ei:
        apply_tool._validate_all_changes(
            db_session, SimpleNamespace(plan_hash="p"), [ch],  # type: ignore[arg-type,list-item]
            {a.id: a}, {})
    assert ei.value.code == "deterministic_action_id_invalid"


def test_all_rows_gate_covers_excluded_no_action(db_session: Session) -> None:  # §2v4
    # excluded_no_action rows exist only in EXCLUDED lanes (which apply skips), so they never become
    # real changes; assert the all-rows gate would nonetheless block one that drifted.
    r, v = _fixture(db_session)
    a = _obs(db_session, r.id, v.id, amount="1.19", observed_at=T0)
    ch = SimpleNamespace(price_observation_id=a.id, action_type="excluded_no_action",
                         deterministic_action_id="d" * 64, actual_after_hash="stale-hash",
                         actual_after_state={"x": 1}, original_hash="r" * 64,
                         lane_fingerprint="lf", created_anomaly_live_id=None)
    run = SimpleNamespace(plan_hash="p")
    with pytest.raises(apply_tool.ApplyRestoreDrift) as ei:
        apply_tool._validate_all_changes(db_session, run, [ch], {a.id: a}, {})  # type: ignore[arg-type,list-item]
    assert ei.value.code == "row_changed_after_apply"


def test_restore_drift_marks_manual_review_durably(committed_dup_lane) -> None:  # §2v4
    slug, rid = committed_dup_lane
    m = _manifest_committed(slug)
    probe = _isession()
    actx = _ctx(probe, m=m)
    probe.close()
    try:
        res = apply_tool.execute_apply(m, actx, authorized=True, confirmations=CONFIRM)
        # Drift a KEEP row from an independent connection, committed.
        d = _isession()
        try:
            run_id = d.execute(select(HistoryRemediationRun.id).where(
                HistoryRemediationRun.plan_hash == m["plan_hash"])).scalar_one()
            keep_obs = d.execute(select(HistoryRemediationChange.price_observation_id).where(
                HistoryRemediationChange.remediation_run_id == run_id,
                HistoryRemediationChange.action_type == "keep")).scalars().first()
            d.execute(text("UPDATE price_observation SET valid_until = :t WHERE id = :i"),
                      {"t": T2, "i": keep_obs})
            d.commit()
        finally:
            d.close()
        probe = _isession()
        rctx = _ctx(probe, m=m)
        probe.close()
        with pytest.raises(apply_tool.ApplyRestoreDrift):
            apply_tool.execute_restore(res["run_public_id"], rctx, authorized=True,
                                       confirmations=RESTORE_CONFIRM)
        run = _run_row(m["plan_hash"], status="applied")  # still applied, not rolled back
        assert run is not None and run["restore_status"] == "manual_review_required"
        assert _rolled_back_count(rid) == 1  # nothing partially restored
    finally:
        _delete_runs(m["plan_hash"])


# --------------------------------------------------------------------------- #
# §3 restore provenance must bind EXACTLY to the run's stored evidence
# --------------------------------------------------------------------------- #
def test_restore_provenance_bound_to_run(committed_dup_lane) -> None:  # §3v4
    slug, _rid = committed_dup_lane
    m = _manifest_committed(slug)
    probe = _isession()
    actx = _ctx(probe, m=m)
    probe.close()
    try:
        res = apply_tool.execute_apply(m, actx, authorized=True, confirmations=CONFIRM)

        def _restore_with(**over):
            probe = _isession()
            c = _ctx(probe, m=m, **over)
            probe.close()
            return apply_tool.execute_restore(res["run_public_id"], c, authorized=True,
                                              confirmations=RESTORE_CONFIRM)

        # A later but internally-valid package -> blocked (bound to the run, not self-coherent).
        with pytest.raises((apply_tool.ApplyProvenanceMismatch, apply_tool.ApplyEnvironmentUnsafe)):
            _restore_with(app_commit_sha=_COMMIT2, deployed_api_sha=_COMMIT2,
                          deployed_worker_sha=_COMMIT2, expected_main_sha=_COMMIT2,
                          observed_provenance=apply_tool.BuildProvenance(
                              _COMMIT2, _SRC2, _API2, _WRK2, _DOC2),
                          expected_provenance=apply_tool.ExpectedProvenance(
                              _COMMIT2, _SRC2, _API2, _WRK2, _DOC2))
        # A different Alembic revision -> blocked.
        with pytest.raises((apply_tool.ApplyProvenanceMismatch, apply_tool.ApplyEnvironmentUnsafe)):
            _restore_with(expected_alembic="not-the-applied-revision")
        # A different provenance document only -> blocked.
        with pytest.raises((apply_tool.ApplyProvenanceMismatch, apply_tool.ApplyEnvironmentUnsafe)):
            _restore_with(observed_provenance=apply_tool.BuildProvenance(
                              _COMMIT, _SRC, _API, _WRK, _DOC2),
                          expected_provenance=apply_tool.ExpectedProvenance(
                              _COMMIT, _SRC, _API, _WRK, _DOC2))
        # A different worker artifact only -> blocked.
        with pytest.raises((apply_tool.ApplyProvenanceMismatch, apply_tool.ApplyEnvironmentUnsafe)):
            _restore_with(observed_provenance=apply_tool.BuildProvenance(
                              _COMMIT, _SRC, _API, _WRK2, _DOC),
                          expected_provenance=apply_tool.ExpectedProvenance(
                              _COMMIT, _SRC, _API, _WRK2, _DOC))
        # The run is still applied; no restore happened.
        run = _run_row(m["plan_hash"], status="applied")
        assert run is not None and run["restore_status"] == "none"
    finally:
        _delete_runs(m["plan_hash"])


# --------------------------------------------------------------------------- #
# §4 storage reference sanitisation
# --------------------------------------------------------------------------- #
def test_sanitize_storage_reference_accepts_opaque_and_uri() -> None:  # §4v4
    assert apply_tool.sanitize_storage_reference("backup-2026-07-27-abc123") == \
        "backup-2026-07-27-abc123"
    uri = "s3://cestaplan-backups/history/2026-07-27.dump"
    assert apply_tool.sanitize_storage_reference(uri) == uri


def test_sanitize_storage_reference_rejects_unsafe() -> None:  # §4v4
    bad = [
        "https://bucket.s3.amazonaws.com/x?X-Amz-Signature=deadbeef",  # signed URL query
        "s3://user:secret@bucket/x",                                    # userinfo
        "s3://bucket/x#frag",                                           # fragment
        "s3://bucket/x?token=abc",                                      # token param
        "s3://bucket/with a space",                                     # unsafe char
        "line\none",                                                    # newline
        "/var/lib/postgresql/backup.dump",                             # local path
        "x" * 201,                                                      # too long
        "s3://bucket/apikey-thing?apikey=z",                            # sensitive param
    ]
    for ref in bad:
        assert apply_tool.sanitize_storage_reference(ref) is None, ref


def test_backup_verify_blocks_unsanitizable_reference() -> None:  # §4v4
    now = datetime.now(UTC)
    be = apply_tool.BackupEvidence(
        path=_BACKUP["path"], expected_sha256=_BACKUP["sha256"], created_at=now,
        expected_postgres_version=_BACKUP["pg_major"],
        storage_reference="https://host/x?X-Amz-Signature=abc")
    ok, ev = be.verify(now, server_version=_BACKUP["pg_major"])
    assert ok is False and ev["reference_sanitized"] is False
    assert ev["storage_reference_sanitized"] is None


def test_run_persists_only_sanitized_reference_and_hash(db_session: Session) -> None:  # §4v4
    _r, _a, _b, _m, _res, run = _apply_dup(db_session)
    ref = "s3://cestaplan-backups/history-remediation.dump"
    assert run.backup_storage_reference == ref  # already sanitized
    assert run.backup_storage_reference_hash == hashlib.sha256(ref.encode()).hexdigest()
    # a local path is NEVER persisted as the reference
    assert run.backup_storage_reference != _BACKUP["path"]


# =========================================================================== #
# v5 FINAL — §1 per-change evidence seal, §2 whole-run seal + pre-restore verify,
# tamper tests, §4 storage reference no disguised paths.
# =========================================================================== #
def _seal_apply(m: dict):
    probe = _isession()
    ctx = _ctx(probe, m=m)
    probe.close()
    return apply_tool.execute_apply(m, ctx, authorized=True, confirmations=CONFIRM)


def _keep_change_info(plan_hash: str) -> dict:
    s = _isession()
    try:
        run_id = s.execute(select(HistoryRemediationRun.id).where(
            HistoryRemediationRun.plan_hash == plan_hash,
            HistoryRemediationRun.status == "applied")).scalar_one()
        ch = s.execute(select(HistoryRemediationChange).where(
            HistoryRemediationChange.remediation_run_id == run_id,
            HistoryRemediationChange.action_type == "keep")).scalars().first()
        assert ch is not None
        return {"run_id": run_id, "change_id": ch.id, "lane_fingerprint": ch.lane_fingerprint,
                "original_hash": ch.original_hash, "action_type": ch.action_type}
    finally:
        s.close()


def _sql_tamper(sql: str, params: dict) -> None:
    s = _isession()
    try:
        s.execute(text(sql), params)
        s.commit()
    finally:
        s.close()


def _assert_restore_blocked_and_manual_review(res, m, rid, *, rolled=1) -> None:
    probe = _isession()
    rctx = _ctx(probe, m=m)
    probe.close()
    with pytest.raises(apply_tool.ApplyRestoreDrift):
        apply_tool.execute_restore(res["run_public_id"], rctx, authorized=True,
                                   confirmations=RESTORE_CONFIRM)
    run = _run_row(m["plan_hash"], status="applied")  # still applied, not rolled back
    assert run is not None and run["restore_status"] == "manual_review_required"
    assert _rolled_back_count(rid) == rolled  # zero rows restored, zero anomalies deleted


def test_tamper_original_temporal_state_blocks(committed_dup_lane) -> None:  # §3v5.1
    slug, rid = committed_dup_lane
    m = _manifest_committed(slug)
    res = _seal_apply(m)
    try:
        info = _keep_change_info(m["plan_hash"])
        _sql_tamper("UPDATE history_remediation_change SET original_temporal_state = "
                "'{\"valid_from\": \"tampered\"}'::jsonb WHERE id = :i", {"i": info["change_id"]})
        _assert_restore_blocked_and_manual_review(res, m, rid)
    finally:
        _delete_runs(m["plan_hash"])


def test_tamper_expected_bound_state_blocks(committed_dup_lane) -> None:  # §3v5.2
    slug, rid = committed_dup_lane
    m = _manifest_committed(slug)
    res = _seal_apply(m)
    try:
        info = _keep_change_info(m["plan_hash"])
        _sql_tamper("UPDATE history_remediation_change SET expected_bound_state = "
                "'{\"valid_from\": \"tampered\"}'::jsonb WHERE id = :i", {"i": info["change_id"]})
        _assert_restore_blocked_and_manual_review(res, m, rid)
    finally:
        _delete_runs(m["plan_hash"])


def test_tamper_actual_after_coherent_blocks(committed_dup_lane) -> None:  # §3v5.3
    slug, rid = committed_dup_lane
    m = _manifest_committed(slug)
    res = _seal_apply(m)
    try:
        info = _keep_change_info(m["plan_hash"])
        state = {"valid_from": "2020-01-01T00:00:00+00:00", "valid_until": None,
                 "verification_status": "unverified", "rolled_back_at": None,
                 "rolled_back_by": None, "closed_by_run_id": None}
        coherent_hash = apply_tool._thash(state)  # state and hash mutually consistent
        _sql_tamper("UPDATE history_remediation_change SET actual_after_state = CAST(:s AS jsonb), "
                    "actual_after_hash = :h WHERE id = :i",
                    {"s": json.dumps(state), "h": coherent_hash, "i": info["change_id"]})
        _assert_restore_blocked_and_manual_review(res, m, rid)
    finally:
        _delete_runs(m["plan_hash"])


def test_tamper_original_hash_and_det_id_coherent_blocks(committed_dup_lane) -> None:  # §3v5.4
    slug, rid = committed_dup_lane
    m = _manifest_committed(slug)
    res = _seal_apply(m)
    try:
        info = _keep_change_info(m["plan_hash"])
        new_hash = "9" * 64
        det = apply_tool._det_action_id(m["plan_hash"], info["lane_fingerprint"], new_hash,
                                        info["action_type"], "")  # keep the det-id gate satisfied
        _sql_tamper("UPDATE history_remediation_change SET original_hash = :o, "
                "deterministic_action_id = :d WHERE id = :i",
                {"o": new_hash, "d": det, "i": info["change_id"]})
        _assert_restore_blocked_and_manual_review(res, m, rid)
    finally:
        _delete_runs(m["plan_hash"])


def test_tamper_lane_fingerprint_and_det_id_coherent_blocks(committed_dup_lane) -> None:  # §3v5.5
    slug, rid = committed_dup_lane
    m = _manifest_committed(slug)
    res = _seal_apply(m)
    try:
        info = _keep_change_info(m["plan_hash"])
        new_lane = "e" * 64
        det = apply_tool._det_action_id(m["plan_hash"], new_lane, info["original_hash"],
                                        info["action_type"], "")
        _sql_tamper("UPDATE history_remediation_change SET lane_fingerprint = :l, "
                "deterministic_action_id = :d WHERE id = :i",
                {"l": new_lane, "d": det, "i": info["change_id"]})
        _assert_restore_blocked_and_manual_review(res, m, rid)
    finally:
        _delete_runs(m["plan_hash"])


def test_tamper_apply_evidence_hash_blocks(committed_dup_lane) -> None:  # §3v5.6
    slug, rid = committed_dup_lane
    m = _manifest_committed(slug)
    res = _seal_apply(m)
    try:
        info = _keep_change_info(m["plan_hash"])
        _sql_tamper("UPDATE history_remediation_change SET apply_evidence_hash = :h WHERE id = :i",
                {"h": "0" * 64, "i": info["change_id"]})
        _assert_restore_blocked_and_manual_review(res, m, rid)
    finally:
        _delete_runs(m["plan_hash"])


def test_tamper_run_execution_hash_blocks(committed_dup_lane) -> None:  # §3v5.7
    slug, rid = committed_dup_lane
    m = _manifest_committed(slug)
    res = _seal_apply(m)
    try:
        _sql_tamper("UPDATE history_remediation_run SET execution_hash = :h WHERE plan_hash = :p "
                "AND status = 'applied'", {"h": "0" * 64, "p": m["plan_hash"]})
        _assert_restore_blocked_and_manual_review(res, m, rid)
    finally:
        _delete_runs(m["plan_hash"])


def test_tamper_consumption_execution_hash_blocks(committed_dup_lane) -> None:  # §3v5.8
    slug, rid = committed_dup_lane
    m = _manifest_committed(slug)
    res = _seal_apply(m)
    try:
        _sql_tamper("UPDATE history_remediation_plan_consumption SET execution_hash = :h "
                "WHERE plan_hash = :p", {"h": "0" * 64, "p": m["plan_hash"]})
        _assert_restore_blocked_and_manual_review(res, m, rid)
    finally:
        _delete_runs(m["plan_hash"])


def test_tamper_consumption_first_run_id_blocks(committed_dup_lane) -> None:  # §3v5.9
    slug, rid = committed_dup_lane
    m = _manifest_committed(slug)
    res = _seal_apply(m)
    try:
        # Point consumption at a DIFFERENT (real, FK-valid) run while keeping execution_hash intact.
        s = _isession()
        try:
            sib = HistoryRemediationRun(
                plan_hash=m["plan_hash"], manifest_schema_version=4, planner_tool_version="t",
                planner_source_hash="s", writer_contract_version="w", main_commit_sha="c",
                alembic_revision="a", execution_mode="apply", status="failed")
            s.add(sib)
            s.flush()
            s.execute(text("UPDATE history_remediation_plan_consumption SET first_run_id = :f "
                           "WHERE plan_hash = :p"), {"f": sib.id, "p": m["plan_hash"]})
            s.commit()
        finally:
            s.close()
        _assert_restore_blocked_and_manual_review(res, m, rid)
    finally:
        _delete_runs(m["plan_hash"])


def test_seal_positive_restore_roundtrip(committed_dup_lane) -> None:  # §3v5 positive
    slug, rid = committed_dup_lane
    m = _manifest_committed(slug)
    res = _seal_apply(m)  # session fully closed by execute_apply
    probe = _isession()
    rctx = _ctx(probe, m=m)
    probe.close()
    rest = apply_tool.execute_restore(res["run_public_id"], rctx, authorized=True,
                                      confirmations=RESTORE_CONFIRM)
    assert rest["status"] == "restored"
    chk = _isession()
    try:
        run = chk.execute(select(HistoryRemediationRun).where(
            HistoryRemediationRun.plan_hash == m["plan_hash"])).scalar_one()
        assert run.status == "rolled_back" and run.restore_status == "restored"
        cons = chk.execute(select(HistoryRemediationPlanConsumption).where(
            HistoryRemediationPlanConsumption.plan_hash == m["plan_hash"])).scalar_one()
        # run + consumption hashes valid (equal) and consumption still present after restore
        assert cons.execution_hash == run.execution_hash and run.execution_hash is not None
    finally:
        chk.close()
    assert _rolled_back_count(rid) == 0  # original state restored exactly
    _delete_runs(m["plan_hash"])


# --------------------------------------------------------------------------- #
# §4v5 storage reference: opaque ids admit no disguised paths
# --------------------------------------------------------------------------- #
def test_sanitize_storage_reference_rejects_disguised_paths() -> None:  # §4v5
    for ref in ("C:/backup.dump", "C:\\backup.dump", "backups/history.dump", "./backup.dump",
                "../backup.dump", "file:///backup.dump", "\\\\host\\share\\backup.dump",
                "s3:backups", "bucket:history"):
        assert apply_tool.sanitize_storage_reference(ref) is None, ref


def test_sanitize_storage_reference_keeps_valid() -> None:  # §4v5
    for ref in ("backup-20260727-abc123", "s3://bucket/history/backup.dump",
                "gs://bucket/history/backup.dump"):
        assert apply_tool.sanitize_storage_reference(ref) == ref, ref


# =========================================================================== #
# provenance v2 — §1 backup bound to the signed package, §3 full build identity,
# and the operational override guard.
# =========================================================================== #
def _obsprov(commit=_COMMIT, alembic=None, trust=None):
    return apply_tool.BuildProvenance(
        commit, _SRC, _API, _WRK, _DOC, alembic_revision=alembic,
        generator_version=_GENVER, authorization_trust_root_hash=trust or _AUTH["trust_hash"],
        pg_restore_binary_sha256=_BACKUP["pg_restore_sha256"], pg_restore_major="18")


def test_backup_gate_passes_when_bound_to_package(db_session: Session) -> None:  # §1.1
    _r, _v = _fixture(db_session)
    _dup_lane(db_session, _r.id, _v.id)
    assert apply_tool._backup_gate(db_session, _ctx(db_session), apply_tool._now_utc())[0] is True


def test_backup_gate_blocks_on_wrong_expected_sha(db_session: Session) -> None:  # §1.2
    _r, m = _dup_manifest(db_session)
    assert "backup_verified" in _blocking(
        db_session, m, _ctx(db_session, expected_backup_sha256="e" * 64))


def test_backup_gate_blocks_on_wrong_reference(db_session: Session) -> None:  # §1.3
    _r, m = _dup_manifest(db_session)
    b = _blocking(db_session, m, _ctx(
        db_session, expected_backup_storage_reference="s3://other-bucket/x.dump",
        expected_backup_storage_reference_hash=hashlib.sha256(b"s3://other-bucket/x.dump").hexdigest()))
    assert "backup_verified" in b


def test_backup_gate_blocks_on_noncanonical_reference(db_session: Session) -> None:  # §1.4
    _r, m = _dup_manifest(db_session)
    variant = _BACKUP_REF.replace("s3://", "S3://")  # equivalent target, non-canonical form
    b = _blocking(db_session, m, _ctx(
        db_session, expected_backup_storage_reference=variant,
        expected_backup_storage_reference_hash=hashlib.sha256(variant.encode()).hexdigest()))
    assert "backup_verified" in b


def test_backup_gate_blocks_without_package_binding(db_session: Session) -> None:  # §1.5
    _r, m = _dup_manifest(db_session)
    b = _blocking(db_session, m, _ctx(
        db_session, expected_backup_sha256=None, expected_backup_storage_reference=None,
        expected_backup_storage_reference_hash=None))
    assert "backup_verified" in b


def test_authorization_identity_persisted_and_sealed(db_session: Session) -> None:  # §1.6
    r, v = _fixture(db_session)
    _dup_lane(db_session, r.id, v.id)
    m = _make_manifest(db_session)
    ctx = _ctx(db_session, m=m)  # valid, fixture-provided authorization (sealed by _apply)
    res = _apply(db_session, m, ctx)
    run = db_session.execute(select(HistoryRemediationRun).where(
        HistoryRemediationRun.plan_hash == m["plan_hash"])).scalar_one()
    assert run.authorization_id == _AUTH_ID
    assert run.authorization_package_hash == ctx.authorization_package_hash
    assert run.authorization_key_fingerprint == _AUTH_FP
    assert run.expected_backup_sha256 == _BACKUP["sha256"]
    assert run.execution_hash == apply_tool._run_execution_hash(run, list(db_session.execute(
        select(HistoryRemediationChange).where(
            HistoryRemediationChange.remediation_run_id == run.id)).scalars()))
    # tampering the persisted authorization id breaks the restore: the package-binding gate (§5v3)
    # trips first (ctx id != run id), before the sealed-evidence recompute.
    db_session.execute(text("UPDATE history_remediation_run SET authorization_id='tampered' "
                            "WHERE id=:i"), {"i": run.id})
    db_session.flush()
    db_session.expire_all()  # force the restore to re-read the tampered row
    with pytest.raises(apply_tool.ApplyProvenanceMismatch) as ei:
        _restore(db_session, res["run_public_id"], _ctx(db_session))
    assert ei.value.code == "restore_provenance_mismatch"


def test_identity_blocks_when_doc_commit_differs_from_app(db_session: Session) -> None:  # §3.1
    _r, m = _dup_manifest(db_session)
    assert "build_doc_commit_matches_app" in _blocking(
        db_session, m, _ctx(db_session, observed_provenance=_obsprov(commit="b" * 40,
                                                                 alembic=_alembic(db_session))))


def test_identity_blocks_when_package_main_ne_expected(db_session: Session) -> None:  # §3.2
    _r, m = _dup_manifest(db_session)
    assert "package_main_matches_expected" in _blocking(
        db_session, m, _ctx(db_session, expected_provenance=apply_tool.ExpectedProvenance(
            "b" * 40, _SRC, _API, _WRK, _DOC)))


def test_identity_blocks_when_doc_alembic_differs_from_package(db_session: Session) -> None:  # §3.3
    _r, m = _dup_manifest(db_session)
    assert "build_doc_alembic_matches_package" in _blocking(
        db_session, m, _ctx(db_session, observed_provenance=_obsprov(alembic="deadbeef")))


def test_identity_blocks_when_doc_alembic_differs_from_live_db(db_session: Session) -> None:  # §3.4
    _r, m = _dup_manifest(db_session)
    # observed==expected==a valid-but-not-live revision -> matches_package ok, matches_live fails
    b = _blocking(db_session, m, _ctx(
        db_session, observed_provenance=_obsprov(alembic="oldrevision"),
        expected_alembic="oldrevision"))
    assert "build_doc_alembic_matches_live" in b
    assert "build_doc_alembic_matches_package" not in b


def test_identity_blocks_when_trust_root_differs(db_session: Session) -> None:  # §3.5
    _r, m = _dup_manifest(db_session)
    assert "trust_root_matches_document" in _blocking(
        db_session, m, _ctx(db_session, observed_provenance=_obsprov(alembic=_alembic(db_session),
                                                                 trust="f" * 64)))


def test_identity_passes_with_exact_full_identity(db_session: Session) -> None:  # §3.6
    _r, v = _fixture(db_session)
    _dup_lane(db_session, _r.id, v.id)
    m = _make_manifest(db_session)
    blocking = _blocking(db_session, m, _ctx(db_session))
    for g in ("build_doc_commit_matches_app", "package_main_matches_expected",
              "build_doc_alembic_matches_package", "build_doc_alembic_matches_live",
              "trust_root_matches_document"):
        assert g not in blocking, g


def test_from_environment_forbids_expected_overrides(monkeypatch) -> None:  # §1 operational guard
    monkeypatch.setenv("APP_COMMIT_SHA", _COMMIT)
    with pytest.raises(apply_tool.ApplyNotAuthorized) as ei:
        apply_tool.ApplyContext.from_environment(plan_hash="d" * 64,
                                                 expected_backup_sha256="e" * 64)
    assert ei.value.code == "override_forbidden_operational_path"


# =========================================================================== #
# provenance v3 — §2 fixed operational paths, §3 explicit authorization gate,
# §5 restore bound to the original package, §6 failed-run audit, §7 toolchain doc.
# =========================================================================== #
def test_cloud_ignores_env_provenance_and_trust_root(monkeypatch) -> None:  # §2v3
    monkeypatch.setenv("DEPLOYMENT_MODE", "production")
    monkeypatch.setenv("BUILD_PROVENANCE_PATH", "/tmp/evil-provenance.json")
    monkeypatch.setenv("BUILD_AUTHORIZATION_TRUST_ROOT_PATH", "/tmp/evil-trust-root.json")
    bp, tr = apply_tool._runtime_provenance_paths()
    assert bp == apply_tool.RUNTIME_BUILD_PROVENANCE_PATH
    assert tr == apply_tool.RUNTIME_AUTHORIZATION_TRUST_ROOT_PATH


def test_self_hosted_uses_env_provenance_paths(monkeypatch) -> None:  # §2v3
    monkeypatch.setenv("DEPLOYMENT_MODE", "self_hosted")
    monkeypatch.setenv("BUILD_PROVENANCE_PATH", "/tmp/bp.json")
    monkeypatch.setenv("BUILD_AUTHORIZATION_TRUST_ROOT_PATH", "/tmp/tr.json")
    assert apply_tool._runtime_provenance_paths() == ("/tmp/bp.json", "/tmp/tr.json")


def test_apply_blocks_without_valid_authorization(db_session: Session) -> None:  # §3v3
    _r, m = _dup_manifest(db_session)
    assert "authz_valid" in _blocking(db_session, m, _ctx(db_session, authorization_valid=False))


def test_apply_blocks_on_expired_authorization(db_session: Session) -> None:  # §3v3
    _r, m = _dup_manifest(db_session)
    past = datetime.now(UTC) - timedelta(minutes=1)
    assert "authz_not_expired" in _blocking(
        db_session, m, _ctx(db_session, authorization_expires_at=past))


def test_apply_blocks_on_stale_generation(db_session: Session) -> None:  # §3v3
    _r, m = _dup_manifest(db_session)
    old = datetime.now(UTC) - timedelta(hours=3)
    assert "authz_generation_fresh" in _blocking(
        db_session, m, _ctx(db_session, authorization_generated_at=old))


def test_apply_blocks_on_plan_hash_mismatch(db_session: Session) -> None:  # §3v3
    _r, m = _dup_manifest(db_session)
    # authorization_plan_hash pre-set (not None) -> the helper won't rebind -> mismatch
    assert "authz_plan_hash_matches" in _blocking(
        db_session, m, _ctx(db_session, authorization_plan_hash="0" * 64))


def test_apply_blocks_on_package_substituted(db_session: Session, monkeypatch, tmp_path) -> None:
    _r, m = _dup_manifest(db_session)  # §2v4
    ctx = _ctx(db_session, m=m)
    _bind_plan(ctx, m["plan_hash"])  # seal a valid package + set ctx identity
    # Substitute the package on disk AFTER ctx was built: a different (bogus) document at the fixed
    # path. The full under-lock re-validation reloads + re-verifies and fails closed.
    swapped = tmp_path / "swapped.json"
    swapped.write_text(json.dumps({"authorization_package_hash": "1" * 64}))
    monkeypatch.setenv("AUTHORIZATION_PACKAGE_PATH", str(swapped))
    now = apply_tool._now_utc()
    gates = dict(apply_tool._authorization_gates(m, ctx, now))
    assert gates["authz_revalidated_under_lock"] is False
    assert apply_tool._authorization_revalidated(ctx, now) is False


def test_fresh_clock_used_under_lock(db_session: Session, monkeypatch) -> None:  # §3v3
    r, v = _fixture(db_session)
    _dup_lane(db_session, r.id, v.id)
    m = _make_manifest(db_session)
    ctx = _ctx(db_session, m=m)  # window valid at build time
    monkeypatch.setattr(apply_tool, "_now_utc", lambda: _AUTH_EXP + timedelta(hours=1))  # jump past
    with pytest.raises(apply_tool.ApplyEnvironmentUnsafe) as ei:
        _apply(db_session, m, ctx)
    assert "authz_not_expired" in str(ei.value)


# ---- §5v3 restore bound to the ORIGINAL signed package ----
def test_restore_blocks_on_different_authorization_id(db_session: Session) -> None:  # §5v3.2
    _r, _a, _b, _m, res, _run = _apply_dup(db_session)
    with pytest.raises(apply_tool.ApplyProvenanceMismatch) as ei:
        _restore(db_session, res["run_public_id"], _ctx(db_session, authorization_id="other-id"))
    assert ei.value.code == "restore_provenance_mismatch"


def test_restore_blocks_on_different_package_hash(db_session: Session) -> None:  # §5v4.3
    _r, _a, _b, _m, res, _run = _apply_dup(db_session)
    # A DIFFERENT but validly-signed package (a different operator_reference changes the self-hash):
    # the under-lock re-validation passes, but the run-binding rejects the different package hash.
    with pytest.raises(apply_tool.ApplyProvenanceMismatch):
        _restore(db_session, res["run_public_id"],
                 _ctx(db_session, operator_reference="ops/ticket-OTHER"))


def test_restore_blocks_on_different_fingerprint(db_session: Session) -> None:  # §5v4.4
    _r, _a, _b, _m, res, run = _apply_dup(db_session)
    # Seal the restore package with the SECOND authorized key (a real, in-trust-root but different
    # fingerprint): re-validation passes, and the run-binding is what rejects the fingerprint.
    ctx = _ctx(db_session)
    _seal_ctx_package(ctx, run.plan_hash, sk=_SK2, fp=_AUTH_FP2)
    with pytest.raises(apply_tool.ApplyProvenanceMismatch):
        _restore(db_session, res["run_public_id"], ctx)


def test_restore_blocks_on_different_dates(db_session: Session) -> None:  # §5v4.5
    _r, _a, _b, _m, res, _run = _apply_dup(db_session)
    with pytest.raises(apply_tool.ApplyProvenanceMismatch):
        _restore(db_session, res["run_public_id"],
                 _ctx(db_session, authorization_generated_at=_AUTH_GEN - timedelta(seconds=30)))


def test_restore_blocks_on_different_expected_backup(db_session: Session) -> None:  # §5v4.6
    _r, _a, _b, _m, res, _run = _apply_dup(db_session)
    # A DIFFERENT but internally-consistent backup binding (ref + matching hash): re-validation
    # passes, and the run-binding rejects the different expected backup.
    other = "s3://other-bucket/x.dump"
    with pytest.raises(apply_tool.ApplyProvenanceMismatch):
        _restore(db_session, res["run_public_id"],
                 _ctx(db_session, expected_backup_sha256="e" * 64,
                      expected_backup_storage_reference=other,
                      expected_backup_storage_reference_hash=hashlib.sha256(other.encode()).hexdigest()))


def test_restore_blocks_on_expired_original_package(db_session: Session) -> None:  # §5v3.7
    _r, _a, _b, _m, res, _run = _apply_dup(db_session)
    expired = datetime.now(UTC) - timedelta(minutes=1)
    with pytest.raises(apply_tool.ApplyEnvironmentUnsafe) as ei:
        _restore(db_session, res["run_public_id"],
                 _ctx(db_session, authorization_expires_at=expired))
    assert ei.value.code == "restore_gates_blocking"


# ---- §6v3 failed-run audit ----
def test_failed_run_preserves_authorization_identity(committed_dup_lane, monkeypatch) -> None:  # §6
    slug, _rid = committed_dup_lane
    m = _manifest_committed(slug)
    probe = _isession()
    ctx = _bind_plan(_ctx(probe, m=m), m["plan_hash"])  # seal a valid package so gates pass
    probe.close()
    monkeypatch.setattr(apply_tool, "_apply_row",
                        lambda *a, **k: _raise(apply_tool.ApplyPlanDrift("injected")))
    s = _isession()
    try:
        with pytest.raises(apply_tool.ApplyPlanDrift):
            apply_tool._apply_guarded(s, m, ctx, authorized=True, confirmations=CONFIRM)
    finally:
        s.rollback()
        s.close()
    monkeypatch.undo()
    try:
        chk = _isession()
        try:
            run = chk.execute(select(HistoryRemediationRun).where(
                HistoryRemediationRun.plan_hash == m["plan_hash"],
                HistoryRemediationRun.status == "failed")).scalar_one()
            assert run.authorization_id == _AUTH_ID
            assert run.authorization_package_hash == ctx.authorization_package_hash
            assert run.expected_backup_sha256 == _BACKUP["sha256"]
            assert run.observed_commit_sha == _COMMIT and run.expected_source_hash == _SRC
            assert planner.scan_sensitive({
                "authorization_id": run.authorization_id,
                "operator_reference": run.operator_reference,
                "error_code": run.error_code}) == []
        finally:
            chk.close()
    finally:
        _delete_runs(m["plan_hash"])


def test_failed_run_null_authorization_when_absent(committed_dup_lane) -> None:  # §6
    slug, _rid = committed_dup_lane
    m = _manifest_committed(slug)
    probe = _isession()
    # a ctx with NO authorization (as if the package never loaded) -> the gates block BEFORE any
    # write, and the durable failed run carries null authorization fields (never invented values).
    ctx = _ctx(probe, m=m, authorization_valid=False, authorization_id=None,
               authorization_package_hash=None, authorization_key_fingerprint=None,
               authorization_generated_at=None, authorization_expires_at=None,
               expected_backup_sha256=None, expected_backup_storage_reference_hash=None)
    probe.close()
    s = _isession()
    try:
        with pytest.raises(apply_tool.ApplyEnvironmentUnsafe):
            apply_tool._apply_guarded(s, m, ctx, authorized=True, confirmations=CONFIRM)
    finally:
        s.rollback()
        s.close()
    try:
        chk = _isession()
        try:
            run = chk.execute(select(HistoryRemediationRun).where(
                HistoryRemediationRun.plan_hash == m["plan_hash"],
                HistoryRemediationRun.status == "failed")).scalar_one()
            assert run.authorization_id is None and run.authorization_package_hash is None
            assert run.expected_backup_sha256 is None and run.error_code == "gates_blocking"
        finally:
            chk.close()
    finally:
        _delete_runs(m["plan_hash"])


# ---- §7v3 full document toolchain validation ----
def _write_doc(tmp_path, **override):
    from cestaplan_api.provenance import generator as g
    doc = {
        "schema_version": 3, "commit_sha": _COMMIT, "source_tree_hash": _SRC,
        "api_artifact_hash": _API, "worker_artifact_hash": _WRK, "alembic_revision": "360a55cb6abb",
        "generator_version": g.GENERATOR_VERSION,
        "toolchain_contract_version": g.TOOLCHAIN_CONTRACT_VERSION,
        "python_base_image_digest": g.PYTHON_BASE_IMAGE_DIGEST,
        "uv_image_digest": g.UV_IMAGE_DIGEST,
        "authorization_trust_root_hash": _AUTH["trust_hash"],
        "postgresql_client_package": "postgresql-client-18",
        "postgresql_client_package_version": "18.4-1.pgdg13+1",
        "pg_restore_major": "18", "pg_restore_version": "18.4",
        "pg_restore_binary_sha256": "d" * 64, "pg_dump_binary_sha256": "e" * 64}
    doc.update(override)
    p = tmp_path / "build-provenance.json"
    p.write_bytes(g.render_document(doc))
    return str(p)


def test_document_valid_loads(tmp_path) -> None:  # §7v3
    o = apply_tool.load_build_provenance(_write_doc(tmp_path))
    assert o.commit_sha == _COMMIT and o.alembic_revision == "360a55cb6abb"


def test_document_wrong_python_digest_rejected(tmp_path) -> None:  # §7v3
    path = _write_doc(tmp_path, python_base_image_digest="sha256:" + "0" * 64)  # valid form, wrong
    assert apply_tool.load_build_provenance(path).commit_sha is None  # fails closed (empty)


def test_document_wrong_uv_digest_rejected(tmp_path) -> None:  # §7v3
    path = _write_doc(tmp_path, uv_image_digest="sha256:" + "0" * 64)
    assert apply_tool.load_build_provenance(path).commit_sha is None


def test_document_wrong_toolchain_contract_rejected(tmp_path) -> None:  # §7v3
    path = _write_doc(tmp_path, toolchain_contract_version="toolchain-v99")
    assert apply_tool.load_build_provenance(path).commit_sha is None


# =========================================================================== #
# provenance v4 — §1 single non-injectable operational clock, §2 full under-lock
# re-validation, §5 audit correctness (no fabricated empty-string evidence).
# =========================================================================== #

# ---- §1v4: one operational clock (_now_utc), ctx.now never used operationally ----
def test_ctx_now_cannot_revive_expired_plan(db_session: Session, monkeypatch) -> None:  # §1v4.1
    _r, m = _dup_manifest(db_session)
    # The operation clock jumps far past the plan's generation; a "fresh" injected ctx.now must NOT
    # be able to revive it — plan age is measured against operation_now only.
    monkeypatch.setattr(apply_tool, "_now_utc", lambda: datetime.now(UTC) + timedelta(days=400))
    assert "plan_not_expired" in _blocking(db_session, m, _ctx(db_session, now=datetime.now(UTC)))


def test_ctx_now_cannot_revive_old_backup(db_session: Session, monkeypatch) -> None:  # §1v4.2
    _r, m = _dup_manifest(db_session)
    monkeypatch.setattr(apply_tool, "_now_utc", lambda: datetime.now(UTC) + timedelta(days=400))
    # backup created_at is ~now; against a far-future operation_now the backup is out of window.
    assert "backup_verified" in _blocking(db_session, m, _ctx(db_session, now=datetime.now(UTC)))


def test_ctx_now_cannot_revive_expired_authorization_on_restore(
        db_session: Session, monkeypatch) -> None:  # §1v4.3
    _r, _a, _b, _m, res, _run = _apply_dup(db_session)
    # After a valid apply, the restore's operation clock jumps past the package's expiry; a fresh
    # injected ctx.now must not revive it.
    monkeypatch.setattr(apply_tool, "_now_utc", lambda: _AUTH_EXP + timedelta(hours=2))
    with pytest.raises(apply_tool.ApplyEnvironmentUnsafe) as ei:
        _restore(db_session, res["run_public_id"], _ctx(db_session, now=datetime.now(UTC)))
    assert ei.value.code == "restore_gates_blocking"


def test_ctx_now_future_does_not_alter_rolled_back_at(db_session: Session) -> None:  # §1v4.4
    r, v = _fixture(db_session)
    _dup_lane(db_session, r.id, v.id)
    m = _make_manifest(db_session)
    future = datetime.now(UTC) + timedelta(days=30)
    _apply(db_session, m, _ctx(db_session, m=m, now=future))
    run = db_session.execute(select(HistoryRemediationRun).where(
        HistoryRemediationRun.plan_hash == m["plan_hash"])).scalar_one()
    rolled = db_session.execute(select(PriceObservation).where(
        PriceObservation.retailer_id == r.id,
        PriceObservation.rolled_back_at.is_not(None))).scalars().all()
    assert rolled  # a duplicate was logically rolled back
    for o in rolled:
        assert o.rolled_back_at is not None
        assert o.rolled_back_at == run.started_at  # the one operation clock, NOT ctx.now
        assert o.rolled_back_at < future


def test_run_ts_equals_operation_now(db_session: Session, monkeypatch) -> None:  # §1v4.5
    r, v = _fixture(db_session)
    _dup_lane(db_session, r.id, v.id)
    m = _make_manifest(db_session)
    # A few seconds ahead of the backup's created_at so the operation clock is within the backup
    # window; ctx.now is set far in the past to prove it is ignored.
    fixed = datetime.now(UTC) + timedelta(seconds=5)
    monkeypatch.setattr(apply_tool, "_now_utc", lambda: fixed)
    _apply(db_session, m, _ctx(db_session, m=m, now=datetime(2000, 1, 1, tzinfo=UTC)))
    run = db_session.execute(select(HistoryRemediationRun).where(
        HistoryRemediationRun.plan_hash == m["plan_hash"])).scalar_one()
    assert run.started_at == fixed and run.completed_at == fixed


def test_failed_run_audit_uses_operation_now(committed_dup_lane, monkeypatch) -> None:  # §1v4.6
    slug, _rid = committed_dup_lane
    m = _manifest_committed(slug)
    probe = _isession()
    ctx = _ctx(probe, m=m)
    probe.close()
    fixed = datetime.now(UTC)
    monkeypatch.setattr(apply_tool, "_now_utc", lambda: fixed)
    monkeypatch.setattr(apply_tool, "_apply_row",
                        lambda *a, **k: _raise(apply_tool.ApplyPlanDrift("injected")))
    s = _isession()
    try:
        with pytest.raises(apply_tool.ApplyPlanDrift):
            apply_tool._apply_guarded(s, m, ctx, authorized=True, confirmations=CONFIRM)
    finally:
        s.rollback()
        s.close()
    monkeypatch.undo()
    try:
        chk = _isession()
        try:
            run = chk.execute(select(HistoryRemediationRun).where(
                HistoryRemediationRun.plan_hash == m["plan_hash"],
                HistoryRemediationRun.status == "failed")).scalar_one()
            assert run.started_at == fixed and run.completed_at == fixed
        finally:
            chk.close()
    finally:
        _delete_runs(m["plan_hash"])


def test_internal_clock_hook_is_monkeypatchable(monkeypatch) -> None:  # §1v4.7
    fixed = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(apply_tool, "_now_utc", lambda: fixed)
    assert apply_tool._now_utc() == fixed


def test_from_environment_forbids_now_override_in_cloud(monkeypatch) -> None:  # §1v4.8
    monkeypatch.setenv("DEPLOYMENT_MODE", "production")
    monkeypatch.setenv("APP_COMMIT_SHA", _COMMIT)
    with pytest.raises(apply_tool.ApplyNotAuthorized) as ei:
        apply_tool.ApplyContext.from_environment(plan_hash="d" * 64, now=datetime.now(UTC))
    assert ei.value.code == "override_forbidden_operational_clock"


# ---- §2v4: full re-validation of the sealed package under the lock ----
def _revalidated(ctx) -> bool:
    return apply_tool._authorization_revalidated(ctx, apply_tool._now_utc())


def _valid_sealed_ctx(db):
    _r, m = _dup_manifest(db)
    ctx = _ctx(db, m=m)  # seals a real valid package matching ctx
    return m, ctx


def test_reval_package_changed_keeping_old_self_hash(db_session: Session) -> None:  # §2v4.1
    _m, ctx = _valid_sealed_ctx(db_session)
    body = json.loads(Path(_AUTH["pkg_path"]).read_bytes())
    body["operator_reference"] = "ops/ticket-MUTATED"  # content changes, self-hash field kept
    Path(_AUTH["pkg_path"]).write_bytes((_canonical(body) + "\n").encode())
    assert _revalidated(ctx) is False


def test_reval_package_resigned_with_unauthorized_key(db_session: Session) -> None:  # §2v4.2
    _m, ctx = _valid_sealed_ctx(db_session)
    rogue, _pk, _fp = _mk_key()  # NOT in the trust-root
    body = Path(_AUTH["pkg_path"]).read_bytes()
    Path(_AUTH["sig_path"]).write_text(rogue.sign(body).hex())
    assert _revalidated(ctx) is False


def test_reval_signature_changed(db_session: Session) -> None:  # §2v4.3
    _m, ctx = _valid_sealed_ctx(db_session)
    Path(_AUTH["sig_path"]).write_text("ab" * 64)  # well-formed but wrong signature
    assert _revalidated(ctx) is False


def test_reval_package_disappears(db_session: Session) -> None:  # §2v4.4
    _m, ctx = _valid_sealed_ctx(db_session)
    Path(_AUTH["pkg_path"]).unlink()
    assert _revalidated(ctx) is False


def test_reval_package_changed_between_reads(db_session: Session, monkeypatch) -> None:  # §2v5
    _m, ctx = _valid_sealed_ctx(db_session)
    # §10v5: the package is read fail-closed via secure_read_bytes (O_NOFOLLOW + fstat around it).
    # Rewrite the file mid-read so the after-read fstat differs -> file_changed_during_read.
    from cestaplan_api.provenance import operational_evidence as oe
    tampered = (_canonical({**json.loads(Path(_AUTH["pkg_path"]).read_bytes()),
                            "operator_reference": "ops/x"}) + "\n").encode()
    real_read = os.read
    state = {"done": False}

    def racing_read(fd, n):
        data = real_read(fd, n)
        if data and not state["done"]:
            state["done"] = True
            Path(_AUTH["pkg_path"]).write_bytes(tampered)  # change size + mtime mid-read
        return data

    monkeypatch.setattr(oe.os, "read", racing_read)
    assert _revalidated(ctx) is False


def test_reval_trust_root_changed(db_session: Session) -> None:  # §2v4.6
    _m, ctx = _valid_sealed_ctx(db_session)
    # Rotate the trust-root to a different key set: the package's signer is no longer authorized.
    _sk, pk, _fp = _mk_key()
    Path(_AUTH["trust_root_path"]).write_text(_canonical(
        {"authorized_ed25519_public_keys": [pk], "schema_version": 1}) + "\n")
    assert _revalidated(ctx) is False


def test_reval_package_expires_after_ctx_before_lock(db_session: Session, monkeypatch) -> None:
    _m, ctx = _valid_sealed_ctx(db_session)  # §2v4.7
    # ctx was built while valid; by the time the lock re-validates, the window has passed.
    monkeypatch.setattr(apply_tool, "_now_utc", lambda: _AUTH_EXP + timedelta(hours=1))
    assert apply_tool._authorization_revalidated(ctx, apply_tool._now_utc()) is False


def test_reval_exact_package_passes(db_session: Session) -> None:  # §2v4.8
    _m, ctx = _valid_sealed_ctx(db_session)
    assert _revalidated(ctx) is True


# ---- §5v4: audit correctness — real observed values or NULL, never "" ----
def test_failed_run_without_observed_commit_persists_null(committed_dup_lane) -> None:  # §5v4
    slug, _rid = committed_dup_lane
    m = _manifest_committed(slug)
    probe = _isession()
    # No package AND no observed commit anywhere -> main_commit_sha must be NULL, never "".
    ctx = _ctx(probe, m=m, authorization_valid=False, authorization_id=None,
               authorization_package_hash=None, authorization_key_fingerprint=None,
               authorization_generated_at=None, authorization_expires_at=None,
               expected_backup_sha256=None, expected_backup_storage_reference_hash=None,
               app_commit_sha=None, observed_provenance=apply_tool.BuildProvenance(
                   None, None, None, None, None, alembic_revision=None,
                   generator_version=None, authorization_trust_root_hash=None))
    probe.close()
    s = _isession()
    try:
        with pytest.raises(apply_tool.ApplyEnvironmentUnsafe):
            apply_tool._apply_guarded(s, m, ctx, authorized=True, confirmations=CONFIRM)
    finally:
        s.rollback()
        s.close()
    try:
        chk = _isession()
        try:
            run = chk.execute(select(HistoryRemediationRun).where(
                HistoryRemediationRun.plan_hash == m["plan_hash"],
                HistoryRemediationRun.status == "failed")).scalar_one()
            assert run.main_commit_sha is None  # real NULL, not ""
            # alembic_revision is read LIVE from the DB during the audit -> a real value here
            assert run.alembic_revision == _alembic(chk)
            assert run.main_commit_sha != "" and run.alembic_revision != ""
        finally:
            chk.close()
    finally:
        _delete_runs(m["plan_hash"])


def test_failed_run_never_stores_empty_string_evidence(committed_dup_lane, monkeypatch) -> None:
    slug, _rid = committed_dup_lane  # §5v4
    m = _manifest_committed(slug)
    probe = _isession()
    ctx = _ctx(probe, m=m)
    probe.close()
    monkeypatch.setattr(apply_tool, "_apply_row",
                        lambda *a, **k: _raise(apply_tool.ApplyPlanDrift("injected")))
    s = _isession()
    try:
        with pytest.raises(apply_tool.ApplyPlanDrift):
            apply_tool._apply_guarded(s, m, ctx, authorized=True, confirmations=CONFIRM)
    finally:
        s.rollback()
        s.close()
    monkeypatch.undo()
    try:
        chk = _isession()
        try:
            run = chk.execute(select(HistoryRemediationRun).where(
                HistoryRemediationRun.plan_hash == m["plan_hash"],
                HistoryRemediationRun.status == "failed")).scalar_one()
            for col in (run.main_commit_sha, run.alembic_revision, run.authorization_id,
                        run.authorization_package_hash):
                assert col != ""  # NULL or a real value, never a fabricated empty string
            assert run.main_commit_sha == _COMMIT  # real observed commit (from APP_COMMIT_SHA)
        finally:
            chk.close()
    finally:
        _delete_runs(m["plan_hash"])
