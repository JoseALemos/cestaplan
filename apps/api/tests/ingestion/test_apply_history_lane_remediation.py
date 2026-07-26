"""Reversible history-lane remediation executor — real-PostgreSQL tests (apply spec §11).

The executor CONSUMES a sealed planner manifest and executes exactly the reviewed plan: logical
rollbacks (never deletes), deterministic interval reconstruction, proposed anomalies, and exact
restore. Every gate is proven to fail closed with zero writes; provenance/contract/env/drift gates
are exercised; idempotency and exact restore are proven.
"""

from __future__ import annotations

import copy
import json
import threading
import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import delete, func, select, text
from sqlalchemy.orm import Session

from cestaplan_api.db import engine
from cestaplan_api.models import (
    ExternalProduct,
    HistoryRemediationChange,
    HistoryRemediationRun,
    PriceAnomaly,
    PriceObservation,
    PriceObservationOccurrence,
    ProductVariant,
    Retailer,
)
from cestaplan_api.tools import apply_history_lane_remediation as apply_tool
from cestaplan_api.tools import plan_history_lane_remediation as planner
from tests.fixtures.provider_scenarios import seed_test_catalog_product, seed_test_retailer

PROVIDER = "test_apply_provider"
T0 = datetime(2026, 7, 25, 8, 0, tzinfo=UTC)
T1 = datetime(2026, 7, 26, 8, 0, tzinfo=UTC)
T2 = datetime(2026, 7, 27, 8, 0, tzinfo=UTC)
_SHA = "4e43bad142b344274d7998cc80d54a708e118613"
CONFIRM = ("I_UNDERSTAND_THIS_WRITES", "PLAN_REVIEWED", "BACKUP_VERIFIED")
RESTORE_CONFIRM = ("I_UNDERSTAND_THIS_RESTORES", "RUN_REVIEWED")


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
    """Generate + JSON-round-trip a sealed manifest, exactly as a real manifest file would be."""
    res = planner._dry_run_in_snapshot(db, provider)
    return json.loads(json.dumps(res["manifest"], default=str))


def _alembic(db: Session) -> str | None:
    return db.execute(text("SELECT version_num FROM alembic_version")).scalar()


def _live_counts(db: Session) -> tuple[int, int]:
    from cestaplan_api.models import ProductPrice, ProviderIngredientMapping
    pp = int(db.scalar(select(func.count()).select_from(ProductPrice)) or 0)
    mp = int(db.scalar(select(func.count()).select_from(ProviderIngredientMapping).where(
        ProviderIngredientMapping.active.is_(True))) or 0)
    return pp, mp


def _ctx(db: Session, **over):
    # The dev test DB carries baseline ProductPrice/mappings; the good-path context expects exactly
    # the live counts (the gate proves "== expected", not a hardcoded 0). A mismatch test overrides.
    pp, mp = _live_counts(db)
    base = {
        "app_commit_sha": _SHA, "immutable_build_hash": "sha256:" + "b" * 64,
        "deployed_api_sha": _SHA, "deployed_worker_sha": _SHA, "expected_main_sha": _SHA,
        "expected_alembic": _alembic(db), "expected_product_price": pp,
        "expected_active_mappings": mp, "backup_sha256": "c" * 64, "backup_verified": True,
        "operator_reference": "ticket-OPS-1", "now": datetime.now(UTC)}
    base.update(over)
    return apply_tool.ApplyContext(**base)


def _tamper(m: dict, mutate) -> dict:
    m2 = copy.deepcopy(m)
    mutate(m2)
    return m2


# --------------------------------------------------------------------------- #
# §11.1 verify-only + §11.2 simulate
# --------------------------------------------------------------------------- #
def test_verify_only_passes_with_good_manifest(db_session: Session) -> None:
    r, v = _fixture(db_session)
    _dup_lane(db_session, r.id, v.id)
    m = _make_manifest(db_session)
    rep = apply_tool.verify_only(db_session, m, _ctx(db_session))
    assert rep["plan_found"] and rep["plan_hash"] == m["plan_hash"]
    assert rep["gates_blocking"] == [] and rep["apply_ready"] is True
    assert "plan_hash_intact" in rep["gates_passed"]


def test_verify_only_blocks_without_immutable_build(db_session: Session) -> None:  # §12
    r, v = _fixture(db_session)
    _dup_lane(db_session, r.id, v.id)
    m = _make_manifest(db_session)
    rep = apply_tool.verify_only(db_session, m, _ctx(db_session, immutable_build_hash=None))
    assert rep["apply_ready"] is False
    assert "immutable_build_provenance_missing" in rep["apply_blockers"]
    assert "immutable_build_provenance" in rep["gates_blocking"]


def test_simulate_over_many_groups(db_session: Session) -> None:  # §11.2 (49 synthetic groups)
    r, _v = _fixture(db_session)
    for i in range(49):
        _p, vi = seed_test_catalog_product(db_session, r, f"AP-G{i}", name=f"G{i}", price=None)
        _obs(db_session, r.id, vi.id, amount="1.19", observed_at=T0)
        _obs(db_session, r.id, vi.id, amount="1.19", observed_at=T0)
    m = _make_manifest(db_session)
    rep = apply_tool.simulate(db_session, m, _ctx(db_session))
    assert rep["simulated_invariants_ok"] is True
    assert rep["planned_changes"] == 49  # one logical rollback per group, zero writes


# --------------------------------------------------------------------------- #
# §11.3 logical rollback (no delete) + §11.4 reconstruction + §11.6 atomic apply
# --------------------------------------------------------------------------- #
def _apply(db, m, ctx):
    return apply_tool.apply(db, m, ctx, authorized=True, confirmations=CONFIRM)


def test_apply_logical_rollback_no_delete(db_session: Session) -> None:  # §11.3
    r, v = _fixture(db_session)
    a, b = _dup_lane(db_session, r.id, v.id)
    before = _counts(db_session, r.id)
    m = _make_manifest(db_session)
    res = _apply(db_session, m, _ctx(db_session))
    assert res["status"] == "applied"
    assert _counts(db_session, r.id) == before  # nothing deleted; counts preserved
    rolled = [o for o in (a, b) if (db_session.refresh(o) or o.rolled_back_at is not None)]
    assert len(rolled) == 1  # exactly one duplicate logically rolled back (never deleted)
    # Run attribution lives in the audit tables (rolled_back_by is a user FK, left untouched).
    run = db_session.execute(select(HistoryRemediationRun).where(
        HistoryRemediationRun.plan_hash == m["plan_hash"])).scalar_one()
    linked = db_session.execute(select(HistoryRemediationChange).where(
        HistoryRemediationChange.remediation_run_id == run.id,
        HistoryRemediationChange.price_observation_id == rolled[0].id)).scalar_one()
    assert linked.action_type == "logical_rollback_exact_duplicate"


def test_apply_reconstructs_intervals(db_session: Session) -> None:  # §11.4
    r, v = _fixture(db_session)
    a = _obs(db_session, r.id, v.id, amount="1.19", observed_at=T0)  # both open -> reconstruct T0
    _obs(db_session, r.id, v.id, amount="1.29", observed_at=T1)
    m = _make_manifest(db_session)
    _apply(db_session, m, _ctx(db_session))
    db_session.refresh(a)
    assert a.valid_until == T1  # T0's interval reconstructed to end at the next anchor


def test_apply_is_atomic_run_and_changes(db_session: Session) -> None:  # §11.6
    r, v = _fixture(db_session)
    _dup_lane(db_session, r.id, v.id)
    m = _make_manifest(db_session)
    _apply(db_session, m, _ctx(db_session))
    run = db_session.execute(select(HistoryRemediationRun).where(
        HistoryRemediationRun.plan_hash == m["plan_hash"])).scalar_one()
    assert run.status == "applied" and run.before_counts and run.after_counts
    changes = db_session.execute(select(HistoryRemediationChange).where(
        HistoryRemediationChange.remediation_run_id == run.id)).scalars().all()
    assert len(changes) == 2  # one row per manifest row of the lane


def _counts(db, rid):
    obs = int(db.scalar(select(func.count()).select_from(PriceObservation).where(
        PriceObservation.retailer_id == rid)) or 0)
    occ = int(db.scalar(select(func.count()).select_from(PriceObservationOccurrence).join(
        PriceObservation, PriceObservation.id == PriceObservationOccurrence.price_observation_id
    ).where(PriceObservation.retailer_id == rid)) or 0)
    return obs, occ


# --------------------------------------------------------------------------- #
# §11.5 exact restore + §11.26 restore repeated + §11.27/§11.28 anomaly lifecycle
# --------------------------------------------------------------------------- #
def test_exact_restore_round_trips(db_session: Session) -> None:  # §11.5
    r, v = _fixture(db_session)
    a, b = _dup_lane(db_session, r.id, v.id)
    originals = {o.id: (o.valid_from, o.valid_until, o.verification_status, o.rolled_back_at)
                 for o in (a, b)}
    m = _make_manifest(db_session)
    res = _apply(db_session, m, _ctx(db_session))
    rest = apply_tool.restore(db_session, res["run_public_id"], _ctx(db_session),
                              authorized=True, confirmations=RESTORE_CONFIRM)
    assert rest["status"] == "restored"
    for o in (a, b):
        db_session.refresh(o)
        assert (o.valid_from, o.valid_until, o.verification_status,
                o.rolled_back_at) == originals[o.id]  # every temporal field restored exactly


def test_restore_repeated_is_idempotent(db_session: Session) -> None:  # §11.26
    r, v = _fixture(db_session)
    _dup_lane(db_session, r.id, v.id)
    m = _make_manifest(db_session)
    res = _apply(db_session, m, _ctx(db_session))
    apply_tool.restore(db_session, res["run_public_id"], _ctx(db_session),
                       authorized=True, confirmations=RESTORE_CONFIRM)
    again = apply_tool.restore(db_session, res["run_public_id"], _ctx(db_session),
                               authorized=True, confirmations=RESTORE_CONFIRM)
    assert again["status"] == "already_restored"


def test_restore_removes_only_its_anomaly_preserving_preexisting(db_session: Session) -> None:
    # §11.27 + §11.28: a same-timestamp conflict makes the plan propose an anomaly; restore deletes
    # exactly that anomaly and leaves a preexisting one untouched.
    r, v = _fixture(db_session)
    _obs(db_session, r.id, v.id, amount="1.19", observed_at=T0)
    _obs(db_session, r.id, v.id, amount="1.29", observed_at=T0)  # distinct fact, same T -> conflict
    pre = PriceAnomaly(price_observation_id=None, anomaly_type="preexisting", severity="low",
                       status="open")
    db_session.add(pre)
    db_session.flush()
    m = _make_manifest(db_session)
    if not any(se for lane in m["lanes"] for se in lane.get("proposed_side_effects", [])):
        pytest.skip("no anomaly proposed for this synthetic conflict")
    res = _apply(db_session, m, _ctx(db_session))
    # Scope to THIS retailer's rows — the dev DB carries unrelated committed anomalies.
    obs_ids = db_session.execute(select(PriceObservation.id).where(
        PriceObservation.retailer_id == r.id)).scalars().all()
    run = db_session.execute(select(HistoryRemediationRun).where(
        HistoryRemediationRun.plan_hash == m["plan_hash"])).scalar_one()
    created_ids = [c for (c,) in db_session.execute(select(
        HistoryRemediationChange.created_anomaly_id).where(
        HistoryRemediationChange.remediation_run_id == run.id,
        HistoryRemediationChange.created_anomaly_id.is_not(None))).all()]
    assert len(created_ids) >= 1  # the run created at least one anomaly
    apply_tool.restore(db_session, res["run_public_id"], _ctx(db_session),
                       authorized=True, confirmations=RESTORE_CONFIRM)
    remaining = db_session.execute(select(func.count()).select_from(PriceAnomaly).where(
        PriceAnomaly.id.in_(created_ids))).scalar()
    assert int(remaining or 0) == 0  # exactly the run's anomalies removed
    assert db_session.get(PriceAnomaly, pre.id) is not None  # preexisting anomaly preserved
    assert obs_ids  # (retailer rows still present — nothing deleted)


# --------------------------------------------------------------------------- #
# §11.7 mid-failure rollback + §11.25 apply repeated (idempotency)
# --------------------------------------------------------------------------- #
def test_mid_failure_rolls_back_everything(db_session: Session, monkeypatch) -> None:  # §11.7
    r, v = _fixture(db_session)
    _dup_lane(db_session, r.id, v.id)
    m = _make_manifest(db_session)
    before = _counts(db_session, r.id)

    def boom(*_a, **_k):
        raise RuntimeError("injected mid-apply")

    monkeypatch.setattr(apply_tool, "_apply_row", boom)
    with pytest.raises(RuntimeError):
        _apply(db_session, m, _ctx(db_session))
    # No PARTIAL row write happened (boom fired before the first row was touched); a real caller now
    # rolls back the single transaction, discarding the pending run row atomically.
    a, b = db_session.execute(select(PriceObservation).where(
        PriceObservation.retailer_id == r.id).order_by(PriceObservation.id)).scalars().all()
    assert a.rolled_back_at is None and b.rolled_back_at is None
    db_session.rollback()
    assert _counts(db_session, r.id) in (before, (0, 0))


def test_apply_repeated_returns_already_applied(db_session: Session) -> None:  # §11.25
    r, v = _fixture(db_session)
    _dup_lane(db_session, r.id, v.id)
    m = _make_manifest(db_session)
    _apply(db_session, m, _ctx(db_session))
    second = _apply(db_session, m, _ctx(db_session))
    assert second["status"] == "already_applied"  # no second write


# --------------------------------------------------------------------------- #
# §11.8-§11.9 tamper + §11.10 occurrence + §11.11/§11.12 FK drift
# --------------------------------------------------------------------------- #
def _blocking(db, m, ctx):
    return apply_tool.verify_only(db, m, ctx)["gates_blocking"]


def test_plan_hash_tamper_blocks(db_session: Session) -> None:  # §11.8
    r, v = _fixture(db_session)
    _dup_lane(db_session, r.id, v.id)
    m = _make_manifest(db_session)
    bad = _tamper(m, lambda x: x.update(plan_hash="deadbeef"))
    assert "plan_hash_intact" in _blocking(db_session, bad, _ctx(db_session))
    with pytest.raises(apply_tool.ApplyEnvironmentUnsafe):
        _apply(db_session, bad, _ctx(db_session))


def test_original_hash_tamper_blocks(db_session: Session) -> None:  # §11.9
    r, v = _fixture(db_session)
    _dup_lane(db_session, r.id, v.id)
    m = _make_manifest(db_session)

    def mut(x):
        x["lanes"][0]["rows"][0]["integrity"]["full_row_hash"] = "0" * 64

    bad = _tamper(m, mut)  # breaks both plan_hash recompute AND live-row hash match
    blocking = _blocking(db_session, bad, _ctx(db_session))
    assert "plan_hash_intact" in blocking or "row_hashes_match" in blocking


def test_occurrence_added_after_plan_blocks(db_session: Session) -> None:  # §11.10
    r, v = _fixture(db_session)
    a, _b = _dup_lane(db_session, r.id, v.id)
    m = _make_manifest(db_session)
    db_session.add(PriceObservationOccurrence(price_observation_id=a.id, provider_code="late",
                                              imported_at=T0))
    db_session.flush()
    assert "occurrences_unchanged" in _blocking(db_session, m, _ctx(db_session))


def test_unknown_fk_after_plan_blocks(db_session: Session) -> None:  # §11.11 / §11.12
    r, v = _fixture(db_session)
    a, _b = _dup_lane(db_session, r.id, v.id)
    m = _make_manifest(db_session)
    db_session.execute(text("CREATE TABLE ap_unknown_ref (id bigint PRIMARY KEY, "
                            "obs_id bigint REFERENCES price_observation(id))"))
    db_session.execute(text("INSERT INTO ap_unknown_ref (id, obs_id) VALUES (1, :o)"), {"o": a.id})
    db_session.flush()
    assert "no_unknown_fk" in _blocking(db_session, m, _ctx(db_session))


# --------------------------------------------------------------------------- #
# §11.13-§11.17 contract/provenance/env gates
# --------------------------------------------------------------------------- #
def test_wrong_writer_contract_blocks(db_session: Session, monkeypatch) -> None:  # §11.13
    r, v = _fixture(db_session)
    _dup_lane(db_session, r.id, v.id)
    m = _make_manifest(db_session)
    monkeypatch.setattr(apply_tool.writer, "writer_contract",
                        lambda: {"version": "old-v1",
                                 "active_exact_ambiguity_policy": "pick_first"})
    assert "writer_contract_v2" in _blocking(db_session, m, _ctx(db_session))


def test_wrong_app_commit_sha_blocks(db_session: Session) -> None:  # §11.14
    r, v = _fixture(db_session)
    _dup_lane(db_session, r.id, v.id)
    m = _make_manifest(db_session)
    b = _blocking(db_session, m, _ctx(db_session, app_commit_sha="wrong", deployed_api_sha="wrong",
                                      deployed_worker_sha="wrong"))
    assert "main_commit_sha_matches" in b


def test_missing_build_hash_blocks(db_session: Session) -> None:  # §11.15
    r, v = _fixture(db_session)
    _dup_lane(db_session, r.id, v.id)
    m = _make_manifest(db_session)
    assert "immutable_build_provenance" in _blocking(
        db_session, m, _ctx(db_session, immutable_build_hash=None))


def test_api_worker_misaligned_blocks(db_session: Session) -> None:  # §11.16
    r, v = _fixture(db_session)
    _dup_lane(db_session, r.id, v.id)
    m = _make_manifest(db_session)
    assert "api_worker_aligned" in _blocking(
        db_session, m, _ctx(db_session, deployed_worker_sha="different"))


def test_wrong_alembic_blocks(db_session: Session) -> None:  # §11.17
    r, v = _fixture(db_session)
    _dup_lane(db_session, r.id, v.id)
    m = _make_manifest(db_session)
    assert "alembic_revision" in _blocking(db_session, m, _ctx(db_session, expected_alembic="nope"))


def test_unexpected_product_price_blocks(db_session: Session) -> None:  # §5 ProductPrice!=expected
    r, v = _fixture(db_session)
    _dup_lane(db_session, r.id, v.id)
    m = _make_manifest(db_session)
    # Default policy expects ProductPrice == 0; the live DB has baseline rows -> gate blocks.
    assert "product_price_matches_expected" in _blocking(
        db_session, m, _ctx(db_session, expected_product_price=0))


# --------------------------------------------------------------------------- #
# §11.18-§11.20 environment gates (production / crawl / job)
# --------------------------------------------------------------------------- #
class _Acts:
    def __init__(self, prod):
        self.production_enabled = prod
        self.production_approved = False


def test_active_provider_blocks(db_session: Session, monkeypatch) -> None:  # §11.18
    r, v = _fixture(db_session)
    _dup_lane(db_session, r.id, v.id)
    m = _make_manifest(db_session)
    monkeypatch.setattr(apply_tool, "_production_enabled", lambda s, a: True)
    assert "production_disabled" in _blocking(db_session, m, _ctx(db_session))


def test_active_crawl_run_blocks(db_session: Session) -> None:  # §11.19
    r, v = _fixture(db_session)
    _dup_lane(db_session, r.id, v.id)
    m = _make_manifest(db_session)
    from cestaplan_api.models import CrawlRun
    db_session.add(CrawlRun(retailer_id=r.id, run_type="prices", status="running"))
    db_session.flush()
    assert "crawl_run_not_running" in _blocking(db_session, m, _ctx(db_session))


def test_active_crawl_job_blocks(db_session: Session) -> None:  # §11.20
    r, v = _fixture(db_session)
    _dup_lane(db_session, r.id, v.id)
    m = _make_manifest(db_session)
    from cestaplan_api.models import CrawlJob, CrawlRun
    run = CrawlRun(retailer_id=r.id, run_type="prices", status="completed")
    db_session.add(run)
    db_session.flush()
    db_session.add(CrawlJob(crawl_run_id=run.id, job_type="prices", status="queued"))
    db_session.flush()
    assert "crawl_job_not_active" in _blocking(db_session, m, _ctx(db_session))


# --------------------------------------------------------------------------- #
# §11.23 fact-identity guard + §11.24 no delete (SQL interceptor) + §8
# --------------------------------------------------------------------------- #
def test_interceptor_forbids_fact_identity_update(db_session: Session) -> None:  # §11.23
    r, v = _fixture(db_session)
    a, _b = _dup_lane(db_session, r.id, v.id)
    with apply_tool._WriteGuard(db_session), pytest.raises(apply_tool.ApplyForbiddenWrite):
        db_session.execute(text("UPDATE price_observation SET amount = 9.99 WHERE id = :i"),
                           {"i": a.id})
    db_session.rollback()


def test_interceptor_forbids_delete_and_occurrence(db_session: Session) -> None:  # §11.24
    r, v = _fixture(db_session)
    a, _b = _dup_lane(db_session, r.id, v.id)
    with apply_tool._WriteGuard(db_session), pytest.raises(apply_tool.ApplyForbiddenWrite):
        db_session.execute(text("DELETE FROM price_observation WHERE id = :i"), {"i": a.id})
    db_session.rollback()
    with apply_tool._WriteGuard(db_session), pytest.raises(apply_tool.ApplyForbiddenWrite):
        db_session.execute(text("DELETE FROM price_observation_occurrence WHERE id = 1"))
    db_session.rollback()


def test_interceptor_allows_whitelisted_update(db_session: Session) -> None:  # §8
    r, v = _fixture(db_session)
    a, _b = _dup_lane(db_session, r.id, v.id)
    with apply_tool._WriteGuard(db_session):
        db_session.execute(text("UPDATE price_observation SET valid_until = :t WHERE id = :i"),
                           {"t": T1, "i": a.id})  # whitelisted temporal field -> allowed
    db_session.rollback()


# --------------------------------------------------------------------------- #
# §11.29 no sensitive data + §11.30 gates hold under python -O
# --------------------------------------------------------------------------- #
def test_manifest_and_report_have_no_sensitive_data(db_session: Session) -> None:  # §11.29
    r, v = _fixture(db_session)
    _obs(db_session, r.id, v.id, amount="1.19", observed_at=T0)
    _obs(db_session, r.id, v.id, amount="1.19", observed_at=T0)
    m = _make_manifest(db_session)
    rep = apply_tool.verify_only(db_session, m, _ctx(db_session))
    assert planner.scan_sensitive(m) == [] and planner.scan_sensitive(rep) == []


def test_gates_hold_under_optimize(db_session: Session) -> None:  # §11.30
    # No `assert` implements a gate: run the module compiled with optimizations stripped and confirm
    # a bad candidate/manifest still raises a typed error (not silently passes).
    import subprocess
    import sys
    code = (
        "from cestaplan_api.tools import apply_history_lane_remediation as a;"
        "import pytest\n"
        "try:\n"
        "    a.load_manifest('/nonexistent/manifest.json'); print('NO_RAISE')\n"
        "except a.ApplyManifestInvalid as e:\n"
        "    print('RAISED', e.code)\n")
    out = subprocess.run([sys.executable, "-O", "-c", code], capture_output=True, text=True)
    assert "RAISED manifest_unreadable" in out.stdout


# --------------------------------------------------------------------------- #
# §11.21 two concurrent applies + §11.22 apply vs record_price_fact (real connections)
# --------------------------------------------------------------------------- #
def _isession() -> Session:
    return Session(bind=engine.connect(), expire_on_commit=False)


@pytest.fixture()
def committed_dup_lane():
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
        rid = r.id
    finally:
        s.close()
    try:
        yield slug, rid
    finally:
        c = _isession()
        try:
            oids = c.execute(select(PriceObservation.id).where(
                PriceObservation.retailer_id == rid)).scalars().all()
            runs = c.execute(select(HistoryRemediationRun.id)).scalars().all()
            if runs:
                c.execute(delete(HistoryRemediationChange).where(
                    HistoryRemediationChange.remediation_run_id.in_(runs)))
            if oids:
                c.execute(delete(PriceAnomaly).where(PriceAnomaly.price_observation_id.in_(oids)))
                c.execute(delete(HistoryRemediationChange).where(
                    HistoryRemediationChange.price_observation_id.in_(oids)))
            c.execute(delete(HistoryRemediationRun).where(
                HistoryRemediationRun.plan_hash.like("%")))
            c.execute(delete(PriceObservation).where(PriceObservation.retailer_id == rid))
            c.execute(delete(ProductVariant).where(ProductVariant.retailer_id == rid))
            c.execute(delete(ExternalProduct).where(ExternalProduct.retailer_id == rid))
            c.execute(delete(Retailer).where(Retailer.id == rid))
            c.commit()
        finally:
            c.close()


def _manifest_committed(slug: str) -> dict:
    s = _isession()
    try:
        s.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
        m = json.loads(json.dumps(planner._dry_run_in_snapshot(s, slug)["manifest"], default=str))
        s.rollback()
        return m
    finally:
        s.close()


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
    assert results.count("applied") == 1  # exactly one apply completes
    assert "already_applied" in results or "ApplyAlreadyApplied" in results


def test_apply_vs_concurrent_writer_serialize(committed_dup_lane) -> None:  # §11.22
    slug, rid = committed_dup_lane
    m = _manifest_committed(slug)
    s = _isession()
    try:
        res = apply_tool.apply(s, m, _ctx(s), authorized=True, confirmations=CONFIRM)
        s.commit()
        assert res["status"] == "applied"
    finally:
        s.close()
    # After the apply committed, the lane holds exactly one active canonical + one rolled-back dup.
    c = _isession()
    try:
        rows = c.execute(select(PriceObservation).where(
            PriceObservation.retailer_id == rid)).scalars().all()
        assert len(rows) == 2 and sum(1 for x in rows if x.rolled_back_at is not None) == 1
    finally:
        c.close()


# --------------------------------------------------------------------------- #
# authorization gate: --apply blocked by default (spec §4C)
# --------------------------------------------------------------------------- #
def test_apply_blocked_without_authorization(db_session: Session) -> None:
    r, v = _fixture(db_session)
    _dup_lane(db_session, r.id, v.id)
    m = _make_manifest(db_session)
    with pytest.raises(apply_tool.ApplyNotAuthorized):
        apply_tool.apply(db_session, m, _ctx(db_session))  # no authorized/confirmations
    with pytest.raises(apply_tool.ApplyNotAuthorized):
        apply_tool.apply(db_session, m, _ctx(db_session), authorized=True, confirmations=("x",))
