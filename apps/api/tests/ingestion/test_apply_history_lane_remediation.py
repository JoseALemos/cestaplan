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
from typing import Any

import pytest
from sqlalchemy import delete, func, select, text
from sqlalchemy.orm import Session

from cestaplan_api.config import Settings
from cestaplan_api.db import engine
from cestaplan_api.models import (
    ExternalProduct,
    HistoryRemediationChange,
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
CONFIRM = ("I_UNDERSTAND_THIS_WRITES", "PLAN_REVIEWED", "BACKUP_VERIFIED")
RESTORE_CONFIRM = ("I_UNDERSTAND_THIS_RESTORES", "RUN_REVIEWED")

_BACKUP: dict[str, str] = {}


@pytest.fixture(scope="module", autouse=True)
def _module_backup():
    """A REAL pg_dump (custom, schema-only) so BackupEvidence.verify() actually passes (§9)."""
    fd, path = tempfile.mkstemp(suffix=".dump")
    os.close(fd)
    uri = Settings().database_url.replace("+psycopg", "")
    subprocess.run(["pg_dump", "-Fc", "--schema-only", "--dbname", uri, "-f", path],
                   check=True, capture_output=True, timeout=120)
    os.chmod(path, 0o600)
    _BACKUP["path"] = path
    _BACKUP["sha256"] = hashlib.sha256(Path(path).read_bytes()).hexdigest()
    yield
    os.unlink(path)


def _backup_evidence(now: datetime) -> Any:
    return apply_tool.BackupEvidence(
        path=_BACKUP["path"], expected_sha256=_BACKUP["sha256"], created_at=now,
        storage_reference="s3://backups/<sanitized>")


def _prov():
    return (apply_tool.BuildProvenance(_COMMIT, _SRC, _API, _WRK, _DOC),
            apply_tool.ExpectedProvenance(_COMMIT, _SRC, _API, _WRK, _DOC))


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


def _ctx(db: Session, *, backup: Any = "default", **over):
    pp, mp = _live_counts(db)
    obs, exp = _prov()
    now = datetime.now(UTC)
    be = _backup_evidence(now) if backup == "default" else backup
    base = {
        "app_commit_sha": _COMMIT, "deployed_api_sha": _COMMIT, "deployed_worker_sha": _COMMIT,
        "expected_main_sha": _COMMIT, "expected_alembic": _alembic(db),
        "observed_provenance": obs, "expected_provenance": exp,
        "expected_product_price": pp, "expected_active_mappings": mp, "backup": be,
        "operator_reference": "ticket-OPS-1", "now": now}
    base.update(over)
    return apply_tool.ApplyContext(**base)


def _verify(db, m, ctx):  # bypasses the public snapshot-pinning for the seeded db_session fixture
    return apply_tool._verify_report(db, m, ctx)


def _blocking(db, m, ctx):
    return _verify(db, m, ctx)["gates_blocking"]


def _apply(db, m, ctx):
    return apply_tool.apply(db, m, ctx, authorized=True, confirmations=CONFIRM)


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
            ctx = _ctx(probe)
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
    assert run.expected_source_hash == _SRC and run.provenance_document_hash == _DOC
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
def _restore(db, run_id, ctx):
    return apply_tool.restore(db, run_id, ctx, authorized=True, confirmations=RESTORE_CONFIRM)


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
    o, e = _prov()
    return _blocking(db, m, _ctx(db, observed_provenance=observed or o,
                                 expected_provenance=expected or e))


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
        apply_tool.apply(db_session, m, _ctx(db_session))
    with pytest.raises(apply_tool.ApplyNotAuthorized):
        apply_tool.apply(db_session, m, _ctx(db_session), authorized=True, confirmations=("x",))


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

    def run():
        s = _isession()
        try:
            barrier.wait(timeout=30)
            res = apply_tool.apply(s, m, _ctx(s), authorized=True, confirmations=CONFIRM)
            s.commit()
            results.append(res["status"])
        except Exception as exc:
            s.rollback()
            results.append(type(exc).__name__)
        finally:
            s.close()

    threads = [threading.Thread(target=run) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert results.count("applied") == 1


def test_apply_then_restore_committed(committed_dup_lane) -> None:  # §11.22 / §6
    slug, rid = committed_dup_lane
    m = _manifest_committed(slug)
    s = _isession()
    try:
        res = apply_tool.apply(s, m, _ctx(s), authorized=True, confirmations=CONFIRM)
        s.commit()
        assert res["status"] == "applied"
    finally:
        s.close()
    c = _isession()
    try:
        rows = c.execute(select(PriceObservation).where(
            PriceObservation.retailer_id == rid)).scalars().all()
        assert len(rows) == 2 and sum(1 for x in rows if x.rolled_back_at is not None) == 1
        rest = apply_tool.restore(c, res["run_public_id"], _ctx(c), authorized=True,
                                  confirmations=RESTORE_CONFIRM)
        c.commit()
        assert rest["status"] == "restored"
    finally:
        c.close()


# uses timedelta to keep the import meaningful for future window tests
_WINDOW = timedelta(hours=6)
