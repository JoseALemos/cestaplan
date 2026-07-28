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
from cestaplan_api.models import (
    HistoryRemediationChange,
    HistoryRemediationPlanConsumption,
    HistoryRemediationRun,
    PriceAnomaly,
    PriceObservation,
    PriceObservationOccurrence,
    ProductPrice,
    ProviderIngredientMapping,
)
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


_FAKE_PG_RESTORE = (
    "#!/bin/sh\n"
    'case "$1" in\n'
    '  --version) echo "pg_restore (PostgreSQL) 18.4";;\n'
    '  --list) echo "; Dumped from database version: 18";;\n'
    "  *) exit 1;;\n"
    "esac\n"
    "exit 0\n"
)


# Canonical runtime-dependency manifest (schema 4) embedded in the synthetic provenance doc.
_PG_RUNTIME_DEPS = [
    {"architecture": "amd64", "package": "libpq5", "version": "18.4-1.pgdg13+1"},
    {"architecture": "amd64", "package": "postgresql-client-18", "version": "18.4-1.pgdg13+1"}]
_PG_RUNTIME_FILES = [
    {"package": "postgresql-client-18", "path": "/usr/lib/postgresql/18/bin/pg_dump",
     "sha256": "a" * 64},
    {"package": "postgresql-client-18", "path": "/usr/lib/postgresql/18/bin/pg_restore",
     "sha256": "b" * 64},
    {"package": "libpq5", "path": "/usr/lib/x86_64-linux-gnu/libpq.so.5.18", "sha256": "c" * 64}]
_PG_RUNTIME_MANIFEST_HASH = "33b37a3e3e3d00bf8a999de6fb275ac2021916a3ffbf13e3087ef2d87905d865"


@pytest.fixture(scope="module", autouse=True)
def _module_backup():
    """Real dump + FAKE pg 18 client. BackupEvidence.verify()'s VerifiedPgRestore opens the fake via
    the self_hosted override; the strict root-owned/ancestor gates are relaxed via
    apply_tool._PG_REQUIRE_ROOT_OWNED=False (never honored in cloud, so production is not weakened)
    and exercised for real in CI's image-runtime job."""
    fd, path = tempfile.mkstemp(suffix=".dump")
    os.close(fd)
    uri = Settings().database_url.replace("+psycopg", "")
    subprocess.run(["pg_dump", "-Fc", "--schema-only", "--dbname", uri, "-f", path],
                   check=True, capture_output=True, timeout=120)
    os.chmod(path, 0o600)
    prfd, prpath = tempfile.mkstemp(suffix="_pg_restore")  # FAKE pinned pg 18 client (0755)
    os.write(prfd, _FAKE_PG_RESTORE.encode())
    os.close(prfd)
    os.chmod(prpath, 0o755)
    _BACKUP["path"] = path
    _BACKUP["sha256"] = hashlib.sha256(Path(path).read_bytes()).hexdigest()
    _BACKUP["pg_major"] = "18"
    _BACKUP["pg_restore_path"] = prpath
    _BACKUP["pg_restore_sha256"] = hashlib.sha256(Path(prpath).read_bytes()).hexdigest()
    _BACKUP["pg_runtime_files"] = ((prpath, _BACKUP["pg_restore_sha256"]),)
    prev = os.environ.get("CESTAPLAN_PG_RESTORE_PATH")
    os.environ["CESTAPLAN_PG_RESTORE_PATH"] = prpath
    real_sv = apply_tool._server_version
    apply_tool._server_version = lambda db: "18"
    real_flag = apply_tool._PG_REQUIRE_ROOT_OWNED
    apply_tool._PG_REQUIRE_ROOT_OWNED = False
    yield
    apply_tool._PG_REQUIRE_ROOT_OWNED = real_flag
    apply_tool._server_version = real_sv
    if prev is None:
        os.environ.pop("CESTAPLAN_PG_RESTORE_PATH", None)
    else:
        os.environ["CESTAPLAN_PG_RESTORE_PATH"] = prev
    os.unlink(path)
    os.unlink(prpath)


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
        "schema_version": 4, "commit_sha": _COMMIT, "source_tree_hash": _SRC,
        "api_artifact_hash": _API, "worker_artifact_hash": _WRK, "alembic_revision": alembic,
        "generator_version": g.GENERATOR_VERSION,
        "toolchain_contract_version": g.TOOLCHAIN_CONTRACT_VERSION,
        "python_base_image_digest": g.PYTHON_BASE_IMAGE_DIGEST,
        "uv_image_digest": g.UV_IMAGE_DIGEST, "authorization_trust_root_hash": trust_hash,
        "postgresql_client_package": "postgresql-client-18",
        "postgresql_client_package_version": "18.4-1.pgdg13+1",
        "pg_restore_major": "18", "pg_restore_version": "18.4",
        "pg_restore_binary_sha256": _BACKUP["pg_restore_sha256"], "pg_dump_binary_sha256": "e" * 64,
        "postgresql_runtime_dependencies": _PG_RUNTIME_DEPS,
        "postgresql_runtime_files": _PG_RUNTIME_FILES,
        "postgresql_runtime_manifest_hash": _PG_RUNTIME_MANIFEST_HASH}
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


_ZERO_WRITE_TABLES = (
    HistoryRemediationRun, HistoryRemediationChange, HistoryRemediationPlanConsumption,
    PriceAnomaly, PriceObservationOccurrence, ProductPrice, ProviderIngredientMapping,
    PriceObservation)


def _all_counts(db) -> dict[str, int]:
    return {t.__name__: int(db.scalar(select(func.count()).select_from(t)) or 0)
            for t in _ZERO_WRITE_TABLES}


def test_verify_ceremony_zero_writes_all_tables(db_session: Session, tmp_path, monkeypatch):
    m, ctx, _ = _build(db_session, tmp_path, monkeypatch)
    before = _all_counts(db_session)
    _report(db_session, m, ctx)
    assert _all_counts(db_session) == before  # every table unchanged


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


def test_prepare_request_zero_writes_all_tables(db_session: Session, tmp_path, monkeypatch):
    m, ctx, _ = _build(db_session, tmp_path, monkeypatch, with_package=False)
    before = _all_counts(db_session)
    _prepare(db_session, m, ctx)
    assert _all_counts(db_session) == before  # every table unchanged


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


# --------------------------------------------------------------------------- #
# §2v2: BackupEvidence.verify over a single securely-opened descriptor
# --------------------------------------------------------------------------- #
import shutil  # noqa: E402

from cestaplan_api.provenance import operational_evidence as oe  # noqa: E402


def _copy_dump(tmp_path, *, mode: int = 0o600) -> str:
    dst = tmp_path / "copy.dump"
    shutil.copy(_BACKUP["path"], dst)
    os.chmod(dst, mode)
    return str(dst)


def _be(path, *, sha=None, ref=_BACKUP_REF, created=None):
    return apply_tool.BackupEvidence(
        path=path, expected_sha256=sha or hashlib.sha256(Path(path).read_bytes()).hexdigest(),
        created_at=created or datetime.now(UTC),
        expected_postgres_version=str(_BACKUP["pg_major"]), storage_reference=ref)


def _verify(be):
    return be.verify(datetime.now(UTC), server_version=str(_BACKUP["pg_major"]),
                     expected_pg_restore_sha256=_BACKUP["pg_restore_sha256"],
                     expected_pg_runtime_files=_BACKUP["pg_runtime_files"])


def _verify_race(be, mutate, monkeypatch):
    real = oe.os.read
    # The pg_restore binary hash and the --version/--list subprocess pipes also flow through
    # os.read; identify the DUMP stream by its content prefix so the mutation races the dump hash
    # (not the binary hash) regardless of how many unrelated reads precede it.
    dump_head = Path(_BACKUP["path"]).read_bytes()[:64]
    state = {"fired": False}

    def racing(fd, n):
        d = real(fd, n)
        if d and not state["fired"] and d[:64] == dump_head:
            state["fired"] = True
            mutate()
        return d

    monkeypatch.setattr(oe.os, "read", racing)
    return be.verify(datetime.now(UTC), server_version=str(_BACKUP["pg_major"]),
                     expected_pg_restore_sha256=_BACKUP["pg_restore_sha256"],
                     expected_pg_runtime_files=_BACKUP["pg_runtime_files"])


def test_backup_valid_dump(tmp_path):
    ok, ev = _verify(_be(_copy_dump(tmp_path)))
    assert ok is True
    assert ev["identity_stable"] is True
    assert ev["pg_restore_list_verified"] is True  # pg_restore read the SAME descriptor


def test_backup_final_symlink_blocks(tmp_path):
    real = _copy_dump(tmp_path)
    link = tmp_path / "link.dump"
    os.symlink(real, link)
    ok, _ev = _verify(_be(str(link)))
    assert ok is False


def test_backup_parent_symlink_blocks(tmp_path):
    d = tmp_path / "real"
    d.mkdir()
    dump = _copy_dump(d)
    linkdir = tmp_path / "linkdir"
    os.symlink(d, linkdir, target_is_directory=True)
    ok, _ev = _verify(_be(str(Path(linkdir) / Path(dump).name)))
    assert ok is False


def test_backup_substitution_blocks(tmp_path, monkeypatch):
    dump = _copy_dump(tmp_path)
    other = tmp_path / "other.dump"
    shutil.copy(_BACKUP["path"], other)
    with open(other, "ab") as f:  # make it a different inode with different content
        f.write(b"\x00extra")
    ok, ev = _verify_race(_be(dump), lambda: os.replace(str(other), dump), monkeypatch)
    assert ok is False and ev["identity_stable"] is False


def test_backup_inplace_same_size_blocks(tmp_path, monkeypatch):
    dump = _copy_dump(tmp_path)
    size = os.path.getsize(dump)

    def mutate():
        with open(dump, "r+b") as f:  # flip a middle byte -> same size, different content
            f.seek(size // 2)
            cur = f.read(1)
            f.seek(size // 2)
            f.write(bytes([cur[0] ^ 0xFF]))
        st = os.stat(dump)  # bump mtime past any 1s filesystem granularity
        os.utime(dump, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))

    ok, ev = _verify_race(_be(dump), mutate, monkeypatch)
    assert ok is False and ev["identity_stable"] is False


def test_backup_truncation_blocks(tmp_path, monkeypatch):
    dump = _copy_dump(tmp_path)
    ok, ev = _verify_race(_be(dump), lambda: os.truncate(dump, 10), monkeypatch)
    assert ok is False and ev["identity_stable"] is False


def test_backup_substitution_before_pg_restore_blocks(tmp_path, monkeypatch):
    dump = _copy_dump(tmp_path)
    other = tmp_path / "other.dump"
    shutil.copy(_BACKUP["path"], other)
    with open(other, "ab") as f:
        f.write(b"\x00x")
    real_run = apply_tool.subprocess.run
    state = {"done": False}

    def racing_run(cmd, *a, **k):
        if not state["done"] and "--list" in cmd:
            state["done"] = True
            os.replace(str(other), dump)  # swap the path right before pg_restore
        return real_run(cmd, *a, **k)

    monkeypatch.setattr(apply_tool.subprocess, "run", racing_run)
    ok, ev = _verify(_be(dump))
    assert ok is False and ev["identity_stable"] is False


def test_backup_open_perms_blocks(tmp_path):
    ok, _ev = _verify(_be(_copy_dump(tmp_path, mode=0o644)))
    assert ok is False


def test_backup_proc_fd_unavailable_fails_closed(tmp_path, monkeypatch):
    dump = _copy_dump(tmp_path)
    real_isdir = os.path.isdir
    monkeypatch.setattr(apply_tool.os.path, "isdir",
                        lambda p: False if p == oe.PROC_SELF_FD else real_isdir(p))
    ok, ev = _verify(_be(dump))
    assert ok is False and ev["pg_restore_list_verified"] is False


def test_backup_path_never_in_report_or_repr(tmp_path):
    dump = _copy_dump(tmp_path)
    be = _be(dump)
    _ok, ev = _verify(be)
    assert dump not in json.dumps(ev, default=str)
    assert dump not in repr(be)


def test_backup_original_never_modified(tmp_path):
    dump = _copy_dump(tmp_path)
    before = Path(dump).read_bytes()
    _verify(_be(dump, sha="e" * 64))  # sha mismatch -> blocks, but must not touch the file
    assert Path(dump).read_bytes() == before


def test_backup_relative_path_blocks(tmp_path):
    ok, _ev = _verify(_be("relative/backup.dump", sha="e" * 64))  # explicit sha; file not read here
    assert ok is False


# --------------------------------------------------------------------------- #
# §5v2: ceremony CLI exit codes (sanitized; no path/secret)
# --------------------------------------------------------------------------- #
def _bad_evidence(tmp_path) -> str:
    p = tmp_path / "bad_evidence.json"
    p.write_text('{"schema_version":1}\n', encoding="utf-8")  # missing required fields
    os.chmod(p, 0o600)
    return str(p)


def test_cli_prepare_invalid_evidence_returns_3_no_output(tmp_path, monkeypatch):
    monkeypatch.setenv("DEPLOYMENT_MODE", "self_hosted")
    manifest = tmp_path / "m.json"
    manifest.write_text(json.dumps({"plan_hash": "d" * 64}))
    out = tmp_path / "request.json"
    rc = apply_tool.main([
        "--manifest-path", str(manifest), "--prepare-authorization-request",
        "--operational-evidence-path", _bad_evidence(tmp_path),
        "--operator-reference", "ops/ticket-1", "--output-path", str(out)])
    assert rc == apply_tool.EXIT_INVALID_INPUT
    assert not out.exists()  # blocked -> never creates the output


def test_cli_verify_ceremony_invalid_evidence_returns_3(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DEPLOYMENT_MODE", "self_hosted")
    manifest = tmp_path / "m.json"
    manifest.write_text(json.dumps({"plan_hash": "d" * 64}))
    pkg = tmp_path / "pkg.json"
    pkg.write_text("{}")
    sig = tmp_path / "pkg.sig"
    sig.write_text("00")
    rc = apply_tool.main([
        "--manifest-path", str(manifest), "--verify-authorization-ceremony",
        "--operational-evidence-path", _bad_evidence(tmp_path),
        "--authorization-package-path", str(pkg), "--authorization-signature-path", str(sig)])
    assert rc == apply_tool.EXIT_INVALID_INPUT
    captured = capsys.readouterr()
    # a JSON report is available and contains no path/secret
    report = json.loads(captured.out)
    assert report["apply_ready"] is False
    for leak in (str(tmp_path), "private", "password", "BEGIN"):
        assert leak not in captured.out and leak not in captured.err


def test_exit_codes_are_stable():
    assert (apply_tool.EXIT_OK, apply_tool.EXIT_GATES_BLOCKING,
            apply_tool.EXIT_INVALID_INPUT, apply_tool.EXIT_UNEXPECTED) == (0, 2, 3, 4)


# --------------------------------------------------------------------------- #
# §2v3: backup strict permissions + versions + second hash
# --------------------------------------------------------------------------- #
def test_backup_chmod_after_open_blocks(tmp_path, monkeypatch):
    dump = _copy_dump(tmp_path)
    real = oe.secure_open_dump

    def wrap(path):
        d = real(path)
        os.fchmod(d.fd, 0o644)  # loosen perms AFTER the secure open
        return d

    monkeypatch.setattr(oe, "secure_open_dump", wrap)
    ok, ev = _verify(_be(dump))
    assert ok is False and ev["permissions_not_public"] is False


def test_backup_db_version_absent_blocks(tmp_path):
    dump = _copy_dump(tmp_path)
    ok, ev = _be(dump).verify(datetime.now(UTC), server_version=None,
                              expected_pg_restore_sha256=_BACKUP["pg_restore_sha256"],
                              expected_pg_runtime_files=_BACKUP["pg_runtime_files"])
    assert ok is False and ev["compatibility_ok"] is False


def test_backup_dump_version_absent_blocks(tmp_path, monkeypatch):
    dump = _copy_dump(tmp_path)
    monkeypatch.setattr(apply_tool, "_dump_db_version", lambda _s: None)
    ok, ev = _verify(_be(dump))
    assert ok is False and ev["compatibility_ok"] is False


def test_backup_pg_restore_version_absent_blocks(tmp_path, monkeypatch):
    dump = _copy_dump(tmp_path)
    real_run = apply_tool.subprocess.run

    def run_hook(cmd, *a, **k):
        r = real_run(cmd, *a, **k)
        if "--version" in cmd:
            r.returncode = 1  # pg_restore --version "fails" -> observed version None
        return r

    monkeypatch.setattr(apply_tool.subprocess, "run", run_hook)
    ok, ev = _verify(_be(dump))
    assert ok is False and ev["observed_pg_restore_version"] is None


def test_backup_version_mismatch_blocks(tmp_path):
    dump = _copy_dump(tmp_path)
    ok, ev = _be(dump).verify(datetime.now(UTC), server_version="99",  # DB major != the rest
                              expected_pg_restore_sha256=_BACKUP["pg_restore_sha256"],
                              expected_pg_runtime_files=_BACKUP["pg_runtime_files"])
    assert ok is False and ev["compatibility_ok"] is False


def test_backup_content_change_during_pg_restore_blocks_via_second_hash(tmp_path, monkeypatch):
    dump = _copy_dump(tmp_path)
    size = os.path.getsize(dump)
    real_run = apply_tool.subprocess.run
    state = {"done": False}

    def run_hook(cmd, *a, **k):
        if not state["done"] and "--list" in cmd:
            state["done"] = True
            with open(dump, "r+b") as f:  # same-size content change during pg_restore
                f.seek(size // 2)
                cur = f.read(1)
                f.seek(size // 2)
                f.write(bytes([cur[0] ^ 0xFF]))
        return real_run(cmd, *a, **k)

    monkeypatch.setattr(apply_tool.subprocess, "run", run_hook)
    ok, ev = _verify(_be(dump))
    assert ok is False and ev["second_sha256_matches"] is False


def test_backup_all_versions_equal_passes(tmp_path):
    ok, ev = _verify(_be(_copy_dump(tmp_path)))
    assert ok is True and ev["compatibility_ok"] is True and ev["second_sha256_matches"] is True


# --------------------------------------------------------------------------- #
# §4v3: manifest under sanitized errors + mode ordering
# --------------------------------------------------------------------------- #
def _write_manifest(tmp_path, obj) -> str:
    p = tmp_path / "m.json"
    p.write_text(obj if isinstance(obj, str) else json.dumps(obj))
    return str(p)


def test_load_ceremony_manifest_valid(tmp_path):
    m = apply_tool._load_ceremony_manifest(_write_manifest(tmp_path, {"plan_hash": "a" * 64}))
    assert m["plan_hash"] == "a" * 64


def test_load_ceremony_manifest_nonexistent(tmp_path):
    with pytest.raises(oe.CeremonyFileError):
        apply_tool._load_ceremony_manifest(str(tmp_path / "nope.json"))


def test_load_ceremony_manifest_malformed(tmp_path):
    with pytest.raises(apply_tool.ApplyManifestInvalid):
        apply_tool._load_ceremony_manifest(_write_manifest(tmp_path, "{not json"))


def test_load_ceremony_manifest_bad_plan_hash(tmp_path):
    with pytest.raises(apply_tool.ApplyManifestInvalid):
        apply_tool._load_ceremony_manifest(_write_manifest(tmp_path, {"plan_hash": "ZZZ"}))


def test_load_ceremony_manifest_symlink_blocks(tmp_path):
    real = _write_manifest(tmp_path, {"plan_hash": "a" * 64})
    link = tmp_path / "link.json"
    os.symlink(real, link)
    with pytest.raises(oe.CeremonyFileError):
        apply_tool._load_ceremony_manifest(str(link))


def test_cli_apply_nonexistent_manifest_aborts_without_reading(tmp_path):
    with pytest.raises(SystemExit) as e:
        apply_tool.main(["--manifest-path", str(tmp_path / "nope.json"), "--apply"])
    assert "not authorized" in str(e.value)


def test_cli_restore_nonexistent_manifest_aborts_without_reading(tmp_path):
    with pytest.raises(SystemExit) as e:
        apply_tool.main(["--manifest-path", str(tmp_path / "nope.json"), "--restore", "x"])
    assert "not authorized" in str(e.value)


def test_cli_prepare_nonexistent_manifest_exit3(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DEPLOYMENT_MODE", "self_hosted")
    ev = _bad_evidence(tmp_path)  # never reached; manifest fails first
    rc = apply_tool.main([
        "--manifest-path", str(tmp_path / "nope.json"), "--prepare-authorization-request",
        "--operational-evidence-path", ev, "--operator-reference", "ops/1",
        "--output-path", str(tmp_path / "out.json")])
    assert rc == apply_tool.EXIT_INVALID_INPUT
    out = capsys.readouterr()
    assert json.loads(out.out)["apply_ready"] is False
    assert str(tmp_path) not in out.out and str(tmp_path) not in out.err


def test_cli_verify_ceremony_malformed_manifest_exit3(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DEPLOYMENT_MODE", "self_hosted")
    manifest = _write_manifest(tmp_path, "{bad")
    pkg = tmp_path / "p.json"
    pkg.write_text("{}")
    sig = tmp_path / "p.sig"
    sig.write_text("00")
    rc = apply_tool.main([
        "--manifest-path", manifest, "--verify-authorization-ceremony",
        "--operational-evidence-path", _bad_evidence(tmp_path),
        "--authorization-package-path", str(pkg), "--authorization-signature-path", str(sig)])
    assert rc == apply_tool.EXIT_INVALID_INPUT
    out = capsys.readouterr()
    assert "Traceback" not in out.err and str(tmp_path) not in (out.out + out.err)
