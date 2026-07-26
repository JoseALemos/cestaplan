"""Read-only history-lane remediation PLANNER (design phase). Proves classification, canonical
policy, dynamic + schema/composite-safe FK discovery, a redacted (no URL/secret) reversible
manifest, a read-only REPEATABLE READ snapshot, a plan_hash sealing full effect content,
residual-free excluded lanes, plan-only apply gating, source provenance, and dry-run runs ONLY
SELECTs and proposes zero deletions."""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import event, func, select, text
from sqlalchemy.exc import InternalError, OperationalError, ProgrammingError
from sqlalchemy.orm import Session

from cestaplan_api.db import engine
from cestaplan_api.models import (
    CrawlRun,
    ExternalProduct,
    PriceObservation,
    PriceObservationOccurrence,
    ProductVariant,
    PromotionRule,
    Retailer,
)
from cestaplan_api.services.observation_persistence import OccurrenceProvenance, record_price_fact
from cestaplan_api.tools import plan_history_lane_remediation as planner
from tests.fixtures.provider_scenarios import seed_test_catalog_product, seed_test_retailer

PROVIDER = "test_plan_provider"
T0 = datetime(2026, 7, 25, 8, 0, tzinfo=UTC)
T1 = datetime(2026, 7, 26, 8, 0, tzinfo=UTC)
T2 = datetime(2026, 7, 27, 8, 0, tzinfo=UTC)
_SHA = "a" * 40


def _fixture(db: Session):
    retailer = seed_test_retailer(db, PROVIDER)
    _p, variant = seed_test_catalog_product(db, retailer, "PL-1", name="Plan", price=None)
    return retailer, variant


def _obs(db, rid, vid, *, amount, observed_at, valid_until=None, imp=None, status="unverified",
         source_url=None):
    o = PriceObservation(
        retailer_id=rid, product_variant_id=vid, price_scope="national", price_type="regular",
        amount=Decimal(amount), currency="EUR", observed_at=observed_at,
        imported_at=imp or observed_at, valid_from=observed_at, valid_until=valid_until,
        confidence_score=Decimal("1.0"), staging_only=True, verification_status=status,
        source_url=source_url)
    db.add(o)
    db.flush()
    return o


def _occ(db, obs_id, *, crawl=None, source=None, provider="p", source_url=None):
    db.add(PriceObservationOccurrence(
        price_observation_id=obs_id, provider_code=provider, crawl_run_id=crawl, source_id=source,
        imported_at=T0, source_url=source_url))
    db.flush()


def _crawl(db, rid) -> int:
    run = CrawlRun(retailer_id=rid, run_type="prices", status="completed")
    db.add(run)
    db.flush()
    return run.id


def _plan(db):
    return planner.dry_run(db, PROVIDER)


def _lane0(db):
    return _plan(db)["manifest"]["lanes"][0]


# --------------------------------------------------------------------------- #
# §1 classification (exact-duplicate before conflict)
# --------------------------------------------------------------------------- #
def test_exact_duplicate_inside_conflict_mandatory_case(db_session: Session) -> None:
    retailer, v = _fixture(db_session)
    _obs(db_session, retailer.id, v.id, amount="1.19", observed_at=T0)
    _obs(db_session, retailer.id, v.id, amount="1.19", observed_at=T0)
    _obs(db_session, retailer.id, v.id, amount="1.29", observed_at=T0)
    r = _plan(db_session)["report"]
    assert r["facts_to_logically_rollback"] == 1
    assert r["facts_to_mark_disputed"] == 2
    assert r["semantic_conflict_groups"] == 1
    assert r["projected_invariants_all_ok"] is True
    acts = [row["action"] for row in _lane0(db_session)["rows"]]
    assert acts.count("logical_rollback_exact_duplicate") == 1
    assert acts.count("mark_disputed_same_timestamp_conflict") == 2


def test_two_dups_of_each_of_two_prices(db_session: Session) -> None:
    retailer, v = _fixture(db_session)
    for amount in ("1.19", "1.19", "1.19", "1.29", "1.29", "1.29"):
        _obs(db_session, retailer.id, v.id, amount=amount, observed_at=T0)
    r = _plan(db_session)["report"]
    assert r["facts_to_logically_rollback"] == 4 and r["facts_to_mark_disputed"] == 2


def test_three_fingerprints_with_internal_dups(db_session: Session) -> None:
    retailer, v = _fixture(db_session)
    for amount in ("1.19", "1.19", "1.29", "1.29", "1.39", "1.39"):
        _obs(db_session, retailer.id, v.id, amount=amount, observed_at=T0)
    r = _plan(db_session)["report"]
    assert r["facts_to_logically_rollback"] == 3 and r["facts_to_mark_disputed"] == 3


def test_human_verified_duplicate_becomes_canonical(db_session: Session) -> None:
    retailer, v = _fixture(db_session)
    low = _obs(db_session, retailer.id, v.id, amount="1.19", observed_at=T0)
    _obs(db_session, retailer.id, v.id, amount="1.19", observed_at=T0, status="human_verified")
    rolled = [row["id"] for row in _lane0(db_session)["rows"]
              if row["action"] == "logical_rollback_exact_duplicate"]
    assert rolled == [low.id]


def test_canonical_prefers_richer_provenance(db_session: Session) -> None:
    retailer, v = _fixture(db_session)
    a = _obs(db_session, retailer.id, v.id, amount="1.19", observed_at=T0)
    b = _obs(db_session, retailer.id, v.id, amount="1.19", observed_at=T0)
    _occ(db_session, b.id, crawl=_crawl(db_session, retailer.id))
    rolled = [row["id"] for row in _lane0(db_session)["rows"]
              if row["action"] == "logical_rollback_exact_duplicate"]
    assert rolled == [a.id]


def test_out_of_order_reconstructed(db_session: Session) -> None:
    retailer, v = _fixture(db_session)
    _obs(db_session, retailer.id, v.id, amount="1.20", observed_at=T2, valid_until=None)
    _obs(db_session, retailer.id, v.id, amount="1.00", observed_at=T0, valid_until=None)
    _obs(db_session, retailer.id, v.id, amount="1.10", observed_at=T1, valid_until=None)
    exp = {row["expected_state_template"]["valid_from"]:
           row["expected_state_template"]["valid_until"] for row in _lane0(db_session)["rows"]}
    assert exp[T0] == T1 and exp[T1] == T2 and exp[T2] is None


# --------------------------------------------------------------------------- #
# §1 no URL / secret in the manifest + recursive scanner
# --------------------------------------------------------------------------- #
def test_manifest_redacts_source_url_and_scan_passes(db_session: Session) -> None:
    retailer, v = _fixture(db_session)
    secret = "https://evil.example.com/x?token=SUPERSECRET123&api_key=abc"
    a = _obs(db_session, retailer.id, v.id, amount="1.19", observed_at=T0, source_url=secret)
    _obs(db_session, retailer.id, v.id, amount="1.19", observed_at=T0)
    _occ(db_session, a.id, crawl=_crawl(db_session, retailer.id), source_url=secret)
    res = _plan(db_session)
    dumped = json.dumps(res["manifest"], default=str)
    assert "SUPERSECRET123" not in dumped and "evil.example.com" not in dumped
    assert res["report"]["output_sensitive_scan_passed"] is True
    assert planner.scan_sensitive(res["manifest"]) == []
    row = next(r for r in _lane0(db_session)["rows"] if r["id"] == a.id)
    assert row["integrity"]["source_url_present"] is True
    assert row["integrity"]["source_url_hash"] is not None
    assert "source_url" not in row["immutable_identity"]


def test_scanner_catches_injected_url() -> None:
    assert planner.scan_sensitive({"x": {"source_url": "https://h/a?token=T"}})
    assert planner.scan_sensitive({"note": "postgresql://u:p@host/db"})
    assert planner.scan_sensitive({"ok": "<redacted>", "n": 1}) == []


# --------------------------------------------------------------------------- #
# §2 read-only REPEATABLE READ snapshot (independent PG sessions)
# --------------------------------------------------------------------------- #
def _isession() -> Session:
    return Session(bind=engine.connect(), expire_on_commit=False)


def _seed_committed():
    slug = f"snap-{datetime.now(UTC).timestamp()}".replace(".", "")
    s = _isession()
    try:
        r = Retailer(slug=slug, name="Snap", adapter_key="test", is_synthetic=True)
        s.add(r)
        s.flush()
        ext = ExternalProduct(retailer_id=r.id, external_id="SN-1")
        s.add(ext)
        s.flush()
        pv = ProductVariant(retailer_id=r.id, external_product_id=ext.id, display_name="V",
                            product_id=None)
        s.add(pv)
        s.flush()
        for _ in range(2):  # two exact dups -> an anomalous lane
            s.add(PriceObservation(
                retailer_id=r.id, product_variant_id=pv.id, price_scope="national",
                price_type="regular", amount=Decimal("1.19"), currency="EUR", observed_at=T0,
                imported_at=T0, valid_from=T0, confidence_score=Decimal("1.0"), staging_only=True))
        s.commit()
        return slug, r.id, pv.id
    finally:
        s.close()


def _cleanup(rid):
    s = _isession()
    try:
        s.execute(text("DELETE FROM price_observation WHERE retailer_id = :r"), {"r": rid})
        s.execute(text("DELETE FROM product_variant WHERE retailer_id = :r"), {"r": rid})
        s.execute(text("DELETE FROM external_product WHERE retailer_id = :r"), {"r": rid})
        s.execute(text("DELETE FROM retailer WHERE id = :r"), {"r": rid})
        s.commit()
    finally:
        s.close()


def test_snapshot_is_read_only_and_rejects_writes() -> None:
    slug, rid, _vid = _seed_committed()
    try:
        s = _isession()
        try:
            res = planner.dry_run(s, slug, snapshot=True)
            assert res["report"]["transaction_read_only"] is True
            assert res["report"]["snapshot_isolation"] == "repeatable read"
            with pytest.raises((InternalError, OperationalError, ProgrammingError)):
                s.execute(text("INSERT INTO retailer (slug, name, adapter_key, public_id) "
                               "VALUES ('x','x','x', gen_random_uuid())"))
            s.rollback()
            assert s.in_transaction() is False
        finally:
            s.close()
    finally:
        _cleanup(rid)


def test_snapshot_keeps_consistent_view_under_concurrent_write() -> None:
    _slug, rid, vid = _seed_committed()
    try:
        s = _isession()
        try:
            planner.readonly_preflight(s)
            before = s.scalar(select(func.count()).select_from(PriceObservation).where(
                PriceObservation.retailer_id == rid))
            w = _isession()
            try:
                w.add(PriceObservation(
                    retailer_id=rid, product_variant_id=vid, price_scope="national",
                    price_type="regular", amount=Decimal("9.99"), currency="EUR", observed_at=T2,
                    imported_at=T2, valid_from=T2, confidence_score=Decimal("1.0"),
                    staging_only=True))
                w.commit()
            finally:
                w.close()
            after = s.scalar(select(func.count()).select_from(PriceObservation).where(
                PriceObservation.retailer_id == rid))
            assert after == before  # REPEATABLE READ snapshot is stable
            s.rollback()
        finally:
            s.close()
    finally:
        _cleanup(rid)


def test_snapshot_dry_run_is_deterministic() -> None:
    slug, rid, _vid = _seed_committed()
    try:
        s1 = _isession()
        s2 = _isession()
        try:
            h1 = planner.dry_run(s1, slug, snapshot=True)["report"]["plan_hash"]
            h2 = planner.dry_run(s2, slug, snapshot=True)["report"]["plan_hash"]
            s1.rollback()
            s2.rollback()
        finally:
            s1.close()
            s2.close()
        assert h1 == h2
    finally:
        _cleanup(rid)


# --------------------------------------------------------------------------- #
# §3 plan_hash seals full effect content
# --------------------------------------------------------------------------- #
def _mini_lane(*, severity="high", restore="delete_only_created_row",
               apply_policy="preserve_unchanged"):
    return {
        "lane_fingerprint": "L1", "excluded": False, "exclusion_reasons": [],
        "rows": [{
            "integrity": {"full_row_hash": "h1"}, "action": "reconstruct_interval",
            "classification": "sequential_unique",
            "expected_state_template": {"valid_from": "T0", "valid_until": None},
            "expected_template_hash": "th1",
            "occurrences": [{"occurrence_hash": "o1"}],
            "incoming_fk_state": [{"full_row_hash": "f1", "apply_policy": apply_policy,
                                   "restore_policy": "preserve_unchanged"}],
        }],
        "proposed_side_effects": [{
            "type": "create_price_anomaly", "anomaly_type": "same_timestamp_conflict",
            "severity": severity, "target_observation_ref": "h1",
            "original_state": "absent", "restore_action": restore,
            "expected_payload_template": {"status": "open"},
            "deterministic_action_id": "aid1"}],
    }


def _seal_of(lane, prereqs=("p1",)):
    return planner._seal("prov", 1, {"a": 1}, [lane], [], {"planner_commit_sha": _SHA},
                         True, ["b1"], list(prereqs), "psh")


def test_seal_changes_with_severity() -> None:
    assert _seal_of(_mini_lane(severity="high")) != _seal_of(_mini_lane(severity="low"))


def test_seal_changes_with_restore_action() -> None:
    assert _seal_of(_mini_lane(restore="delete_only_created_row")) != _seal_of(
        _mini_lane(restore="noop"))


def test_seal_changes_with_apply_policy() -> None:
    assert _seal_of(_mini_lane(apply_policy="preserve_unchanged")) != _seal_of(
        _mini_lane(apply_policy="rewrite"))


def test_seal_changes_with_prerequisite() -> None:
    assert _seal_of(_mini_lane(), prereqs=("p1",)) != _seal_of(_mini_lane(), prereqs=("p2",))


def test_seal_stable_and_order_independent() -> None:
    lane = _mini_lane()
    assert _seal_of(lane) == _seal_of(lane)
    lane2 = _mini_lane()
    lane2["proposed_side_effects"] = list(reversed(lane2["proposed_side_effects"]))
    assert _seal_of(lane) == _seal_of(lane2)


def test_plan_hash_sensitivity_from_data(db_session: Session) -> None:
    retailer, v = _fixture(db_session)
    a = _obs(db_session, retailer.id, v.id, amount="1.19", observed_at=T0)
    b = _obs(db_session, retailer.id, v.id, amount="1.19", observed_at=T0)
    h0 = _plan(db_session)["report"]["plan_hash"]
    _occ(db_session, min(a.id, b.id), crawl=_crawl(db_session, retailer.id))
    h1 = _plan(db_session)["report"]["plan_hash"]
    assert h1 != h0
    db_session.add(PromotionRule(price_observation_id=a.id, type="percentage"))
    db_session.flush()
    assert _plan(db_session)["report"]["plan_hash"] != h1


# --------------------------------------------------------------------------- #
# §4 excluded lanes: zero residual actions, template == original
# --------------------------------------------------------------------------- #
def _assert_excluded_clean(lane) -> None:
    assert lane["excluded"] is True and lane["apply_allowed"] is False
    assert lane["proposed_actions"] == [] and lane["proposed_side_effects"] == []
    for row in lane["rows"]:
        assert row["action"] == "excluded_no_action"
        assert row["classification"] == "excluded"
        assert row["expected_state_template"] == row["original_temporal_state"]
        assert row["expected_template_hash"] == row["integrity"]["full_row_hash"]


def test_excluded_human_verified_conflict(db_session: Session) -> None:
    retailer, v = _fixture(db_session)
    _obs(db_session, retailer.id, v.id, amount="1.19", observed_at=T0, status="human_verified")
    _obs(db_session, retailer.id, v.id, amount="1.29", observed_at=T0)
    lane = next(x for x in _plan(db_session)["manifest"]["lanes"] if x["excluded"])
    _assert_excluded_clean(lane)
    assert "human_reviewed_conflict" in lane["exclusion_reasons"]


def test_excluded_unknown_fk(db_session: Session) -> None:
    retailer, v = _fixture(db_session)
    a = _obs(db_session, retailer.id, v.id, amount="1.19", observed_at=T0)
    _obs(db_session, retailer.id, v.id, amount="1.19", observed_at=T0)
    db_session.execute(text("CREATE TABLE synth_ref (id bigint PRIMARY KEY, "
                            "obs_id bigint REFERENCES price_observation(id))"))
    db_session.execute(text("INSERT INTO synth_ref (id, obs_id) VALUES (1, :o)"), {"o": a.id})
    db_session.flush()
    lane = next(x for x in _plan(db_session)["manifest"]["lanes"] if x["excluded"])
    _assert_excluded_clean(lane)
    assert any(x.startswith("uncovered_fk:") for x in lane["exclusion_reasons"])


def test_excluded_null_timestamp(db_session: Session, monkeypatch) -> None:
    retailer, v = _fixture(db_session)
    _obs(db_session, retailer.id, v.id, amount="1.19", observed_at=T0)
    _obs(db_session, retailer.id, v.id, amount="1.19", observed_at=T0)
    real_load = planner._load

    def patched(db, provider):
        lanes, occ, sfk, ufk, rid, disc = real_load(db, provider)
        for rows in lanes.values():
            db.expunge(rows[0])  # detach so mutating it does not autoflush a NULL to the DB
            rows[0].observed_at = None  # type: ignore[assignment]  # null anchor forces exclusion
        return lanes, occ, sfk, ufk, rid, disc

    monkeypatch.setattr(planner, "_load", patched)
    lane = next(x for x in _plan(db_session)["manifest"]["lanes"] if x["excluded"])
    _assert_excluded_clean(lane)
    assert "null_timestamp" in lane["exclusion_reasons"]


# --------------------------------------------------------------------------- #
# §5 FK discovery schema-safe + composite-safe
# --------------------------------------------------------------------------- #
def test_all_model_fks_have_handlers() -> None:
    assert planner.metadata_fk_tables() <= planner._HANDLED_FK_TABLES


def test_known_fk_without_rows_does_not_exclude(db_session: Session) -> None:
    retailer, v = _fixture(db_session)
    _obs(db_session, retailer.id, v.id, amount="1.19", observed_at=T0)
    _obs(db_session, retailer.id, v.id, amount="1.19", observed_at=T0)
    assert _plan(db_session)["report"]["lanes_excluded"] == 0


def test_unknown_fk_with_quoted_name_excludes(db_session: Session) -> None:
    retailer, v = _fixture(db_session)
    a = _obs(db_session, retailer.id, v.id, amount="1.19", observed_at=T0)
    _obs(db_session, retailer.id, v.id, amount="1.19", observed_at=T0)
    db_session.execute(text('CREATE TABLE "Synth Ref" (id bigint PRIMARY KEY, '
                            "obs_id bigint REFERENCES price_observation(id))"))
    db_session.execute(text('INSERT INTO "Synth Ref" (id, obs_id) VALUES (1, :o)'), {"o": a.id})
    db_session.flush()
    r = _plan(db_session)["report"]
    assert "Synth Ref" in r["fk_unknown"] and r["lanes_excluded"] == 1


def test_discovery_records_only_the_price_observation_column(db_session: Session) -> None:
    # A table with TWO single FKs (one to price_observation.id, one to retailer.id): discovery must
    # record ONLY the price_observation column. (A true composite FK to price_observation is
    # impossible — no composite unique key — but the zip pairing in the code is composite-safe.)
    retailer, v = _fixture(db_session)
    a = _obs(db_session, retailer.id, v.id, amount="1.19", observed_at=T0)
    _obs(db_session, retailer.id, v.id, amount="1.19", observed_at=T0)
    db_session.execute(text(
        "CREATE TABLE multi_ref (k int PRIMARY KEY, "
        "obs_id bigint REFERENCES price_observation(id), ret_id bigint REFERENCES retailer(id))"))
    db_session.execute(text("INSERT INTO multi_ref (k, obs_id, ret_id) VALUES (1, :o, :r)"),
                       {"o": a.id, "r": retailer.id})
    db_session.flush()
    cols = [f["column"] for f in planner.discover_incoming_fks(db_session)
            if f["table"] == "multi_ref"]
    assert cols == ["obs_id"]  # ret_id (-> retailer) is NOT recorded
    assert _plan(db_session)["report"]["lanes_excluded"] == 1


# --------------------------------------------------------------------------- #
# §6 static plan-only gating + §7 provenance
# --------------------------------------------------------------------------- #
def test_apply_never_ready_plan_only(db_session: Session, monkeypatch) -> None:
    monkeypatch.setenv("PLANNER_COMMIT_SHA", _SHA)
    monkeypatch.setenv("DATABASE_CODE_SHA", _SHA)
    monkeypatch.setenv("BASE_MAIN_SHA", _SHA)
    retailer, v = _fixture(db_session)
    _obs(db_session, retailer.id, v.id, amount="1.19", observed_at=T0)
    _obs(db_session, retailer.id, v.id, amount="1.19", observed_at=T0)
    r = _plan(db_session)["report"]
    assert r["apply_ready"] is False
    assert "planner_is_plan_only" in r["apply_blockers"]
    assert "record_price_fact_rolled_back_reuse_not_remediated" in r["apply_blockers"]
    assert "unknown_commit_provenance" not in r["apply_blockers"]
    assert r["writer_contract_status"] == "unverified"


def test_short_sha_is_incomplete_provenance(db_session: Session, monkeypatch) -> None:
    monkeypatch.setenv("PLANNER_COMMIT_SHA", "d71c356")  # short -> not exact
    monkeypatch.delenv("DATABASE_CODE_SHA", raising=False)
    monkeypatch.delenv("RAILWAY_GIT_COMMIT_SHA", raising=False)
    monkeypatch.delenv("BASE_MAIN_SHA", raising=False)
    retailer, v = _fixture(db_session)
    _obs(db_session, retailer.id, v.id, amount="1.19", observed_at=T0)
    _obs(db_session, retailer.id, v.id, amount="1.19", observed_at=T0)
    r = _plan(db_session)["report"]
    assert r["commit_provenance_complete"] is False
    assert "unknown_commit_provenance" in r["apply_blockers"]


def test_planner_source_hash_present(db_session: Session) -> None:
    retailer, v = _fixture(db_session)
    _obs(db_session, retailer.id, v.id, amount="1.19", observed_at=T0)
    _obs(db_session, retailer.id, v.id, amount="1.19", observed_at=T0)
    assert len(_plan(db_session)["report"]["planner_source_hash"]) == 64


def test_record_price_fact_may_reuse_rolled_back(db_session: Session) -> None:
    retailer, v = _fixture(db_session)
    rb = _obs(db_session, retailer.id, v.id, amount="1.19", observed_at=T0)
    rb.rolled_back_at = T0
    db_session.flush()
    _obs(db_session, retailer.id, v.id, amount="1.19", observed_at=T0)
    candidate = PriceObservation(
        retailer_id=retailer.id, product_variant_id=v.id, price_scope="national",
        price_type="regular", amount=Decimal("1.19"), currency="EUR", observed_at=T0,
        imported_at=T0, valid_from=T0, confidence_score=Decimal("1.0"), staging_only=True)
    res = record_price_fact(db_session, candidate, OccurrenceProvenance(provider_code="x"),
                            imported_at=T0)
    assert res.observation.rolled_back_at is not None or res.observation.id == rb.id


# --------------------------------------------------------------------------- #
# read-only proof
# --------------------------------------------------------------------------- #
@contextmanager
def _capture_sql(db: Session):
    stmts: list[str] = []
    conn = db.connection()

    def _before(conn, cursor, statement, params, context, executemany):
        stmts.append(statement)

    event.listen(conn.engine, "before_cursor_execute", _before)
    try:
        yield stmts
    finally:
        event.remove(conn.engine, "before_cursor_execute", _before)


def test_dry_run_is_select_only_and_zero_deletes(db_session: Session) -> None:
    retailer, v = _fixture(db_session)
    _obs(db_session, retailer.id, v.id, amount="1.19", observed_at=T0)
    _obs(db_session, retailer.id, v.id, amount="1.19", observed_at=T0)
    before = int(db_session.scalar(select(func.count()).select_from(PriceObservation).where(
        PriceObservation.retailer_id == retailer.id)) or 0)
    with _capture_sql(db_session) as stmts:
        result = planner.dry_run(db_session, PROVIDER)
    writes = [s for s in stmts if s.lstrip()[:6].upper() in ("INSERT", "UPDATE", "DELETE")]
    assert writes == []
    actions = {row["action"] for lane in result["manifest"]["lanes"] for row in lane["rows"]}
    assert "delete" not in " ".join(actions).lower()
    after = int(db_session.scalar(select(func.count()).select_from(PriceObservation).where(
        PriceObservation.retailer_id == retailer.id)) or 0)
    assert after == before
