"""Verify-only authorization-ceremony adapter — real-PostgreSQL tests.

Covers ApplyContext.from_ceremony_files (expected from the signed package, observed from the
operational evidence, paths pinned), the request preparation (canonical/deterministic/read-only,
no forbidden fields, no backup path), and the full ceremony verification (apply_ready=true only when
every gate is valid; fail-closed otherwise). The adapter NEVER signs, decrypts a key, imports a
private key, or writes remediation data — the only keys here are EPHEMERAL test keys."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from cestaplan_api.config import Settings
from cestaplan_api.db import engine
from cestaplan_api.models import PriceObservation, ProductPrice, ProviderIngredientMapping
from cestaplan_api.provenance import generator as g
from cestaplan_api.tools import apply_history_lane_remediation as apply_tool
from cestaplan_api.tools import plan_history_lane_remediation as planner
from tests.fixtures.provider_scenarios import seed_test_catalog_product, seed_test_retailer

PROVIDER = "test_ceremony_provider"
T0 = datetime(2026, 7, 25, 8, 0, tzinfo=UTC)
_COMMIT = "1" * 40
_SRC, _API, _WRK = "a" * 64, "b" * 64, "c" * 64
_BACKUP_REF = "s3://cestaplan-backups/ceremony.dump"

_SK = Ed25519PrivateKey.generate()
_PK_HEX = _SK.public_key().public_bytes(
    serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()
_FP = hashlib.sha256(bytes.fromhex(_PK_HEX)).hexdigest()[:16]
_SK2 = Ed25519PrivateKey.generate()  # an UNauthorized key (not in the trust-root)

_BACKUP: dict[str, Any] = {}


def _isession() -> Session:
    return Session(bind=engine.connect(), expire_on_commit=False)


@pytest.fixture(scope="module", autouse=True)
def _module_backup():
    fd, path = tempfile.mkstemp(suffix=".dump")
    os.close(fd)
    uri = Settings().database_url.replace("+psycopg", "")
    subprocess.run(["pg_dump", "-Fc", "--schema-only", "--dbname", uri, "-f", path],
                   check=True, capture_output=True, timeout=120)
    os.chmod(path, 0o600)
    _BACKUP["path"] = path
    _BACKUP["sha256"] = hashlib.sha256(Path(path).read_bytes()).hexdigest()
    probe = _isession()
    try:
        _BACKUP["pg_major"] = apply_tool._major(
            probe.execute(text("SHOW server_version")).scalar())
    finally:
        probe.close()
    yield
    os.unlink(path)


def _canon(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _fixture(db: Session):
    retailer = seed_test_retailer(db, PROVIDER)
    _p, variant = seed_test_catalog_product(db, retailer, "CER-1", name="Ceremony", price=None)
    return retailer, variant


def _dup_lane(db, rid, vid):
    for _ in range(2):
        o = PriceObservation(
            retailer_id=rid, product_variant_id=vid, price_scope="national", price_type="regular",
            amount=Decimal("1.19"), currency="EUR", requires_loyalty=False, observed_at=T0,
            imported_at=T0, valid_from=T0, confidence_score=Decimal("1.0"), staging_only=True,
            verification_status="unverified")
        db.add(o)
    db.flush()


def _make_manifest(db: Session) -> dict:
    return json.loads(json.dumps(
        planner._dry_run_in_snapshot(db, PROVIDER)["manifest"], default=str))


def _live_counts(db: Session) -> tuple[int, int]:
    pp = int(db.scalar(select(func.count()).select_from(ProductPrice)) or 0)
    mp = int(db.scalar(select(func.count()).select_from(ProviderIngredientMapping).where(
        ProviderIngredientMapping.active.is_(True))) or 0)
    return pp, mp


def _write_doc(d: Path, trust_hash: str, *, alembic: str) -> tuple[str, str]:
    doc = {
        "schema_version": 2, "commit_sha": _COMMIT, "source_tree_hash": _SRC,
        "api_artifact_hash": _API, "worker_artifact_hash": _WRK, "alembic_revision": alembic,
        "generator_version": g.GENERATOR_VERSION,
        "toolchain_contract_version": g.TOOLCHAIN_CONTRACT_VERSION,
        "python_base_image_digest": g.PYTHON_BASE_IMAGE_DIGEST,
        "uv_image_digest": g.UV_IMAGE_DIGEST, "authorization_trust_root_hash": trust_hash}
    raw = g.render_document(doc)
    p = d / "build-provenance.json"
    p.write_bytes(raw)
    return str(p), hashlib.sha256(raw).hexdigest()


def _write_evidence(d: Path, *, api_sha: str, worker_sha: str, backup_sha: str,
                    ref: str) -> str:
    ev = {
        "schema_version": 1, "deployed_api_sha": api_sha, "deployed_worker_sha": worker_sha,
        "backup": {
            "path": _BACKUP["path"], "expected_sha256": backup_sha,
            "created_at": datetime.now(UTC).isoformat(),
            "expected_postgres_version": str(_BACKUP["pg_major"]), "storage_reference": ref}}
    p = d / "evidence.json"
    p.write_text(_canon(ev) + "\n", encoding="utf-8")
    os.chmod(p, 0o600)
    return str(p)


def _seal_package(d: Path, *, plan_hash: str, doc_hash: str, alembic: str, pp: int, mp: int,
                  sk: Ed25519PrivateKey = _SK, backup_sha: str | None = None,
                  ref: str = _BACKUP_REF, expires_in: int = 1800,
                  alembic_pkg: str | None = None) -> tuple[str, str]:
    pkg = {
        "schema_version": 1,
        "authorization_id": "remediation-ceremony-001",
        "plan_hash": plan_hash,
        "main_commit_sha": _COMMIT,
        "alembic_revision": alembic_pkg or alembic,
        "expected_commit_sha": _COMMIT,
        "expected_source_hash": _SRC,
        "expected_api_artifact_hash": _API,
        "expected_worker_artifact_hash": _WRK,
        "expected_document_hash": doc_hash,
        "expected_product_price": pp,
        "expected_active_mappings": mp,
        "generated_at": (datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
        "expires_at": (datetime.now(UTC) + timedelta(seconds=expires_in)).isoformat(),
        "operator_reference": "ops/ticket-CEREMONY",
        "backup_expected_sha256": backup_sha or _BACKUP["sha256"],
        "backup_storage_reference": ref,
    }
    pkg["authorization_package_hash"] = hashlib.sha256(_canon(pkg).encode()).hexdigest()
    body = (_canon(pkg) + "\n").encode()
    pp_path = d / "package.json"
    sig_path = d / "package.sig"
    pp_path.write_bytes(body)
    sig_path.write_text(sk.sign(body).hex())
    return str(pp_path), str(sig_path)


def _build(db, tmp_path, monkeypatch, *, keys: list[str] | None = None, with_package: bool = True,
           **seal_over) -> tuple[dict, apply_tool.ApplyContext, dict]:
    r, v = _fixture(db)
    _dup_lane(db, r.id, v.id)
    m = _make_manifest(db)
    pp, mp = _live_counts(db)
    live_alembic = db.execute(text("SELECT version_num FROM alembic_version")).scalar()
    d = tmp_path
    tr = d / "trust-root.json"
    tr.write_text(_canon({"authorized_ed25519_public_keys": keys if keys is not None else [_PK_HEX],
                          "schema_version": 1}) + "\n")
    trust_hash = hashlib.sha256(tr.read_bytes()).hexdigest()
    doc_path, doc_hash = _write_doc(d, trust_hash, alembic=live_alembic)
    ev_path = _write_evidence(
        d, api_sha=seal_over.pop("evidence_api", _COMMIT),
        worker_sha=seal_over.pop("evidence_worker", _COMMIT),
        backup_sha=seal_over.pop("evidence_backup_sha", _BACKUP["sha256"]),
        ref=seal_over.pop("evidence_ref", _BACKUP_REF))
    monkeypatch.setenv("DEPLOYMENT_MODE", "self_hosted")
    monkeypatch.setenv("BUILD_PROVENANCE_PATH", doc_path)
    monkeypatch.setenv("BUILD_AUTHORIZATION_TRUST_ROOT_PATH", str(tr))
    monkeypatch.setenv("APP_COMMIT_SHA", _COMMIT)
    pkg_path = sig_path = None
    if with_package:
        pkg_path, sig_path = _seal_package(
            d, plan_hash=m["plan_hash"], doc_hash=doc_hash, alembic=live_alembic, pp=pp, mp=mp,
            **seal_over)
    ctx = apply_tool.ApplyContext.from_ceremony_files(
        plan_hash=m["plan_hash"], operational_evidence_path=ev_path,
        authorization_package_path=pkg_path, authorization_signature_path=sig_path)
    paths = {"pkg": pkg_path, "sig": sig_path, "evidence": ev_path, "doc": doc_path,
             "trust": str(tr), "backup": _BACKUP["path"]}
    return m, ctx, paths


def _report(db, m, ctx) -> dict:
    return apply_tool._verify_report(db, m, ctx)


# --------------------------------------------------------------------------- #
# Ceremony verification (§11)
# --------------------------------------------------------------------------- #
def test_valid_package_and_backup_apply_ready_true(db_session: Session, tmp_path, monkeypatch):
    m, ctx, _ = _build(db_session, tmp_path, monkeypatch)
    rep = _report(db_session, m, ctx)
    assert rep["apply_ready"] is True, rep["gates_blocking"] + rep["apply_blockers"]


def test_package_absent_false(db_session: Session, tmp_path, monkeypatch):
    m, ctx, _ = _build(db_session, tmp_path, monkeypatch, with_package=False)
    assert _report(db_session, m, ctx)["apply_ready"] is False


def test_signature_altered_false(db_session: Session, tmp_path, monkeypatch):
    m, _ctx, paths = _build(db_session, tmp_path, monkeypatch)
    Path(paths["sig"]).write_text("ab" * 64)  # well-formed but wrong signature
    ctx2 = apply_tool.ApplyContext.from_ceremony_files(
        plan_hash=m["plan_hash"], operational_evidence_path=paths["evidence"],
        authorization_package_path=paths["pkg"], authorization_signature_path=paths["sig"])
    assert _report(db_session, m, ctx2)["apply_ready"] is False


def test_unauthorized_key_false(db_session: Session, tmp_path, monkeypatch):
    # package signed with _SK2, but the trust-root only authorizes _PK_HEX
    m, ctx, _ = _build(db_session, tmp_path, monkeypatch, sk=_SK2)
    assert _report(db_session, m, ctx)["apply_ready"] is False


def test_backup_hash_different_false(db_session: Session, tmp_path, monkeypatch):
    m, ctx, _ = _build(db_session, tmp_path, monkeypatch, backup_sha="e" * 64)
    rep = _report(db_session, m, ctx)
    assert rep["apply_ready"] is False
    assert "verified_backup_missing" in rep["apply_blockers"]


def test_storage_reference_different_false(db_session: Session, tmp_path, monkeypatch):
    m, ctx, _ = _build(db_session, tmp_path, monkeypatch, ref="s3://other-bucket/x.dump")
    assert _report(db_session, m, ctx)["apply_ready"] is False


def test_api_worker_sha_different_false(db_session: Session, tmp_path, monkeypatch):
    m, ctx, _ = _build(db_session, tmp_path, monkeypatch, evidence_worker="2" * 40)
    assert _report(db_session, m, ctx)["apply_ready"] is False


def test_alembic_different_false(db_session: Session, tmp_path, monkeypatch):
    m, ctx, _ = _build(db_session, tmp_path, monkeypatch, alembic_pkg="deadbeef")
    assert _report(db_session, m, ctx)["apply_ready"] is False


def test_expired_package_false(db_session: Session, tmp_path, monkeypatch):
    m, ctx, _ = _build(db_session, tmp_path, monkeypatch, expires_in=-30)
    assert _report(db_session, m, ctx)["apply_ready"] is False


def test_package_substituted_under_lock_false(db_session: Session, tmp_path, monkeypatch):
    _m, ctx, paths = _build(db_session, tmp_path, monkeypatch)
    # a DIFFERENT (bogus) package at the pinned path after the context was built
    Path(paths["pkg"]).write_text(json.dumps({"authorization_package_hash": "1" * 64}))
    now = apply_tool._now_utc()
    assert apply_tool._authorization_revalidated(ctx, now) is False


def test_env_change_cannot_redirect_revalidation(db_session: Session, tmp_path, monkeypatch):
    _m, ctx, _paths = _build(db_session, tmp_path, monkeypatch)
    # point the ENV at a bogus package; the ceremony ctx must ignore it and use its pinned files
    bogus = tmp_path / "bogus.json"
    bogus.write_text(json.dumps({"authorization_package_hash": "1" * 64}))
    monkeypatch.setenv("AUTHORIZATION_PACKAGE_PATH", str(bogus))
    monkeypatch.setenv("AUTHORIZATION_SIGNATURE_PATH", str(bogus))
    assert apply_tool._authorization_revalidated(ctx, apply_tool._now_utc()) is True


def test_context_reveals_no_paths(db_session: Session, tmp_path, monkeypatch):
    _m, ctx, paths = _build(db_session, tmp_path, monkeypatch)
    r = repr(ctx)
    for key in ("pkg", "sig", "evidence", "doc", "trust", "backup"):
        assert paths[key] not in r, key


def test_verify_ceremony_zero_writes(db_session: Session, tmp_path, monkeypatch):
    m, ctx, _ = _build(db_session, tmp_path, monkeypatch)
    before = int(db_session.scalar(select(func.count()).select_from(PriceObservation)) or 0)
    _report(db_session, m, ctx)
    after = int(db_session.scalar(select(func.count()).select_from(PriceObservation)) or 0)
    assert before == after


# --------------------------------------------------------------------------- #
# Request preparation (§7/§11)
# --------------------------------------------------------------------------- #
def _prepare(db, m, ctx):
    return apply_tool._prepare_request_report(db, m, ctx, operator_reference="ops/ticket-42")


def test_prepare_request_canonical_and_fields(db_session: Session, tmp_path, monkeypatch):
    m, ctx, _ = _build(db_session, tmp_path, monkeypatch, with_package=False)
    out = _prepare(db_session, m, ctx)
    assert out["prepared"] is True, out["request_blockers"]
    req = out["request"]
    # canonical + deterministic request_hash over everything but request_hash
    h = hashlib.sha256(_canon({k: v for k, v in req.items() if k != "request_hash"}).encode())
    assert req["request_hash"] == h.hexdigest()
    assert req["authorized_key_fingerprint"] == _FP
    assert req["plan_hash"] == m["plan_hash"]
    assert req["expected_commit_sha"] == _COMMIT
    # NONE of the signer-only / secret fields are present
    for forbidden in ("generated_at", "expires_at", "authorization_package_hash", "signature",
                      "private_key", "password", "backup_path", "path", "database_url"):
        assert forbidden not in req
    # the local backup path never appears anywhere in the serialized request
    assert _BACKUP["path"] not in _canon(req)


def test_prepare_request_deterministic(db_session: Session, tmp_path, monkeypatch):
    m, ctx, _ = _build(db_session, tmp_path, monkeypatch, with_package=False)
    a = _prepare(db_session, m, ctx)["request"]
    b = _prepare(db_session, m, ctx)["request"]
    assert _canon(a) == _canon(b)


def test_prepare_request_zero_writes(db_session: Session, tmp_path, monkeypatch):
    m, ctx, _ = _build(db_session, tmp_path, monkeypatch, with_package=False)
    before = int(db_session.scalar(select(func.count()).select_from(PriceObservation)) or 0)
    _prepare(db_session, m, ctx)
    after = int(db_session.scalar(select(func.count()).select_from(PriceObservation)) or 0)
    assert before == after


def test_prepare_request_blocks_on_backup_mismatch(db_session: Session, tmp_path, monkeypatch):
    # evidence points at a backup whose expected sha does not match the real dump -> backup fails
    m, ctx, _ = _build(db_session, tmp_path, monkeypatch, with_package=False,
                       evidence_backup_sha="f" * 64)
    out = _prepare(db_session, m, ctx)
    assert out["prepared"] is False
    assert "backup_verified" in out["request_blockers"]


def test_prepare_request_blocks_on_drift(db_session: Session, tmp_path, monkeypatch):
    m, ctx, _ = _build(db_session, tmp_path, monkeypatch, with_package=False)
    # mutate a planned row's action AFTER the manifest was sealed -> plan_hash / drift blocks
    m2 = json.loads(json.dumps(m))
    m2["plan_hash"] = "0" * 64
    out = _prepare(db_session, m2, ctx)
    assert out["prepared"] is False


# --------------------------------------------------------------------------- #
# CLI stays write-blocked (§8/§11)
# --------------------------------------------------------------------------- #
def _cli(argv, env):
    e = {**os.environ, **env}
    return subprocess.run(
        ["python", "-m", "cestaplan_api.tools.apply_history_lane_remediation", *argv],
        capture_output=True, text=True, env=e, timeout=60,
        cwd=str(Path(__file__).resolve().parents[2]))


def test_cli_apply_restore_simulate_blocked(tmp_path):
    manifest = tmp_path / "m.json"
    manifest.write_text(json.dumps({"plan_hash": "d" * 64}))
    cloud = {"DEPLOYMENT_MODE": "cloud"}
    r_apply = _cli(["--manifest-path", str(manifest), "--apply"], cloud)
    assert r_apply.returncode != 0 and "not authorized" in (r_apply.stderr + r_apply.stdout)
    r_restore = _cli(["--manifest-path", str(manifest), "--restore", "x"], cloud)
    assert r_restore.returncode != 0 and "not authorized" in (r_restore.stderr + r_restore.stdout)
    r_sim = _cli(["--manifest-path", str(manifest), "--simulate"], cloud)
    assert r_sim.returncode != 0 and "not allowed in cloud" in (r_sim.stderr + r_sim.stdout)


def test_cli_has_no_apply_writer_in_ceremony_modes():
    src = Path(apply_tool.__file__).read_text()
    # the ceremony/prepare code paths must never call the writing entrypoints
    prepare_and_verify = src[src.index("def prepare_authorization_request"):
                             src.index("def _acquire(")]
    for banned in ("_apply_locked(", "_restore_locked(", "execute_apply", "execute_restore",
                   "Ed25519PrivateKey", ".sign("):
        assert banned not in prepare_and_verify, banned
