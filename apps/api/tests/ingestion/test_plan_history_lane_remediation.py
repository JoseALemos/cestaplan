"""Read-only history-lane remediation PLANNER (design phase). Proves: exact-duplicate-before-
conflict classification, a stable canonical policy, dynamic incoming-FK discovery, full dependency
state in a reversible manifest, a plan_hash sealing every input, template-vs-bound hashing,
differentiated commit provenance, apply-readiness gating, the record_price_fact reuse blocker,
excluded lanes kept in the manifest, an in-memory simulator holding every invariant, idempotence,
and — critically — that the dry-run executes ONLY SELECTs and proposes zero deletions."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import event, func, select, text
from sqlalchemy.orm import Session

from cestaplan_api.models import (
    CrawlRun,
    PriceAnomaly,
    PriceObservation,
    PriceObservationOccurrence,
    PromotionRule,
)
from cestaplan_api.services import observation_identity as ident
from cestaplan_api.services.observation_persistence import OccurrenceProvenance, record_price_fact
from cestaplan_api.tools import plan_history_lane_remediation as planner
from tests.fixtures.provider_scenarios import seed_test_catalog_product, seed_test_retailer

PROVIDER = "test_plan_provider"  # not in the matrix -> slug == provider (isolated retailer)
T0 = datetime(2026, 7, 25, 8, 0, tzinfo=UTC)
T1 = datetime(2026, 7, 26, 8, 0, tzinfo=UTC)
T2 = datetime(2026, 7, 27, 8, 0, tzinfo=UTC)


def _fixture(db: Session):
    retailer = seed_test_retailer(db, PROVIDER)
    _p, variant = seed_test_catalog_product(db, retailer, "PL-1", name="Plan", price=None)
    return retailer, variant


def _obs(db, rid, vid, *, amount, observed_at, valid_until=None, imp=None, status="unverified"):
    o = PriceObservation(
        retailer_id=rid, product_variant_id=vid, price_scope="national", price_type="regular",
        amount=Decimal(amount), currency="EUR", observed_at=observed_at,
        imported_at=imp or observed_at, valid_from=observed_at, valid_until=valid_until,
        confidence_score=Decimal("1.0"), staging_only=True, verification_status=status,
    )
    db.add(o)
    db.flush()
    return o


def _occ(db, obs_id, *, crawl=None, source=None, provider: str | None = "p"):
    db.add(PriceObservationOccurrence(
        price_observation_id=obs_id, provider_code=provider, crawl_run_id=crawl, source_id=source,
        imported_at=T0))
    db.flush()


def _run(db, rid) -> int:
    run = CrawlRun(retailer_id=rid, run_type="prices", status="completed")
    db.add(run)
    db.flush()
    return run.id


def _plan(db):
    return planner.dry_run(db, PROVIDER)


def _lane0(db):
    return _plan(db)["manifest"]["lanes"][0]


def _actions(lane):
    return [row["action"] for row in lane["rows"]]


# --------------------------------------------------------------------------- #
# §1 exact-duplicate-before-conflict classification
# --------------------------------------------------------------------------- #
def test_exact_duplicate_inside_conflict_mandatory_case(db_session: Session) -> None:
    retailer, v = _fixture(db_session)
    _obs(db_session, retailer.id, v.id, amount="1.19", observed_at=T0)  # A
    _obs(db_session, retailer.id, v.id, amount="1.19", observed_at=T0)  # B, exact dup of A
    _obs(db_session, retailer.id, v.id, amount="1.29", observed_at=T0)  # C
    r = _plan(db_session)["report"]
    assert r["facts_to_logically_rollback"] == 1  # exactly one of A/B
    assert r["facts_to_mark_disputed"] == 2  # canonical of 1.19 + C
    assert r["semantic_conflict_groups"] == 1
    assert r["projected_invariants_all_ok"] is True
    # No row ever receives two actions (each row has exactly one action).
    acts = _actions(_lane0(db_session))
    assert acts.count("logical_rollback_exact_duplicate") == 1
    assert acts.count("mark_disputed_same_timestamp_conflict") == 2


def test_two_dups_of_each_of_two_prices(db_session: Session) -> None:
    retailer, v = _fixture(db_session)
    for amount in ("1.19", "1.19", "1.19", "1.29", "1.29", "1.29"):  # 3 each at T0
        _obs(db_session, retailer.id, v.id, amount=amount, observed_at=T0)
    r = _plan(db_session)["report"]
    assert r["facts_to_logically_rollback"] == 4  # 2 non-canonical per price
    assert r["facts_to_mark_disputed"] == 2  # one canonical per price, both disputed
    assert r["semantic_conflict_groups"] == 1


def test_three_fingerprints_with_internal_dups(db_session: Session) -> None:
    retailer, v = _fixture(db_session)
    for amount in ("1.19", "1.19", "1.29", "1.29", "1.39", "1.39"):
        _obs(db_session, retailer.id, v.id, amount=amount, observed_at=T0)
    r = _plan(db_session)["report"]
    assert r["facts_to_logically_rollback"] == 3
    assert r["facts_to_mark_disputed"] == 3


def test_human_verified_duplicate_becomes_canonical(db_session: Session) -> None:
    retailer, v = _fixture(db_session)
    low = _obs(db_session, retailer.id, v.id, amount="1.19", observed_at=T0)  # lower id, unverified
    _obs(db_session, retailer.id, v.id, amount="1.19", observed_at=T0, status="human_verified")
    lane = _lane0(db_session)
    rolled = [row["id"] for row in lane["rows"]
              if row["action"] == "logical_rollback_exact_duplicate"]
    assert rolled == [low.id]  # the human-verified duplicate is kept canonical, never rolled back


def test_human_verified_canonical_in_conflict_is_excluded(db_session: Session) -> None:
    retailer, v = _fixture(db_session)
    _obs(db_session, retailer.id, v.id, amount="1.19", observed_at=T0, status="human_verified")
    _obs(db_session, retailer.id, v.id, amount="1.29", observed_at=T0)  # conflict at T0
    r = _plan(db_session)["report"]
    assert r["lanes_excluded"] == 1
    assert "human_reviewed_conflict" in r["exclusion_reasons"]
    assert r["manual_review_required"] >= 1


# --------------------------------------------------------------------------- #
# canonical policy + reconstruction + idempotence
# --------------------------------------------------------------------------- #
def test_canonical_prefers_richer_provenance(db_session: Session) -> None:
    retailer, v = _fixture(db_session)
    a = _obs(db_session, retailer.id, v.id, amount="1.19", observed_at=T0)
    b = _obs(db_session, retailer.id, v.id, amount="1.19", observed_at=T0)
    _occ(db_session, b.id, crawl=_run(db_session, retailer.id))
    lane = _lane0(db_session)
    rolled = [row["id"] for row in lane["rows"]
              if row["action"] == "logical_rollback_exact_duplicate"]
    assert rolled == [a.id]


def test_canonical_tiebreak_is_lowest_id(db_session: Session) -> None:
    retailer, v = _fixture(db_session)
    a = _obs(db_session, retailer.id, v.id, amount="1.19", observed_at=T0)
    b = _obs(db_session, retailer.id, v.id, amount="1.19", observed_at=T0)
    lane = _lane0(db_session)
    rolled = [row["id"] for row in lane["rows"]
              if row["action"] == "logical_rollback_exact_duplicate"]
    assert rolled == [max(a.id, b.id)]


def test_six_exact_duplicates(db_session: Session) -> None:
    retailer, v = _fixture(db_session)
    for _ in range(6):
        _obs(db_session, retailer.id, v.id, amount="1.19", observed_at=T0, valid_until=None)
    r = _plan(db_session)["report"]
    assert r["facts_to_logically_rollback"] == 5
    assert r["projected_invariants_all_ok"] is True and r["lanes_plannable"] == 1


def test_out_of_order_reconstructed(db_session: Session) -> None:
    retailer, v = _fixture(db_session)
    _obs(db_session, retailer.id, v.id, amount="1.20", observed_at=T2, valid_until=None)
    _obs(db_session, retailer.id, v.id, amount="1.00", observed_at=T0, valid_until=None)
    _obs(db_session, retailer.id, v.id, amount="1.10", observed_at=T1, valid_until=None)
    lane = _lane0(db_session)
    exp = {
        row["expected_state_template"]["valid_from"]: row["expected_state_template"]["valid_until"]
        for row in lane["rows"]
    }
    assert exp[T0] == T1 and exp[T1] == T2 and exp[T2] is None


def test_clean_lane_has_zero_planned_changes(db_session: Session) -> None:
    retailer, v = _fixture(db_session)
    _obs(db_session, retailer.id, v.id, amount="1.00", observed_at=T0, valid_until=T1)
    _obs(db_session, retailer.id, v.id, amount="1.10", observed_at=T1, valid_until=None)
    assert _plan(db_session)["report"]["lanes_plannable"] == 0


# --------------------------------------------------------------------------- #
# §2 dynamic FK discovery
# --------------------------------------------------------------------------- #
def test_all_model_fks_to_price_observation_have_handlers() -> None:
    # Guard: a new model FK to price_observation without a handler fails this test.
    assert planner.metadata_fk_tables() <= planner._HANDLED_FK_TABLES


def test_incoming_promotion_rule_preserved(db_session: Session) -> None:
    retailer, v = _fixture(db_session)
    a = _obs(db_session, retailer.id, v.id, amount="1.19", observed_at=T0)
    _obs(db_session, retailer.id, v.id, amount="1.19", observed_at=T0)
    db_session.add(PromotionRule(price_observation_id=a.id, type="percentage"))
    db_session.flush()
    r = _plan(db_session)["report"]
    assert r["lanes_excluded"] == 0
    assert r["fk_dependencies_in_planned_lanes"] >= 1
    # The FK's full state is captured in the manifest (not just a count).
    fk = next(row["incoming_fk_state"] for row in _lane0(db_session)["rows"] if row["id"] == a.id)
    assert fk and fk[0]["table"] == "promotion_rule" and "original_values" in fk[0]


def test_dynamic_discovery_of_synthetic_fk_excludes_lane(db_session: Session) -> None:
    retailer, v = _fixture(db_session)
    a = _obs(db_session, retailer.id, v.id, amount="1.19", observed_at=T0)
    _obs(db_session, retailer.id, v.id, amount="1.19", observed_at=T0)  # anomalous lane
    # A REAL synthetic table with an FK to price_observation.id (discovered, no monkeypatch).
    db_session.execute(text(
        "CREATE TABLE synth_obs_ref (id bigint PRIMARY KEY, "
        "obs_id bigint REFERENCES price_observation(id))"))
    db_session.execute(text("INSERT INTO synth_obs_ref (id, obs_id) VALUES (1, :o)"), {"o": a.id})
    db_session.flush()
    r = _plan(db_session)["report"]
    assert "synth_obs_ref" in r["fk_unknown"]
    assert r["lanes_excluded"] == 1
    assert any(reason.startswith("uncovered_fk:") for reason in r["exclusion_reasons"])
    assert r["manual_review_required"] >= 1


# --------------------------------------------------------------------------- #
# §3 full dependency state + proposed side effect
# --------------------------------------------------------------------------- #
def test_existing_price_anomaly_preserved_and_distinguished(db_session: Session) -> None:
    retailer, v = _fixture(db_session)
    a = _obs(db_session, retailer.id, v.id, amount="1.19", observed_at=T0)
    _obs(db_session, retailer.id, v.id, amount="1.19", observed_at=T0)
    db_session.add(PriceAnomaly(price_observation_id=a.id, anomaly_type="stale", severity="low"))
    db_session.flush()
    lane = _lane0(db_session)
    fk = next(row["incoming_fk_state"] for row in lane["rows"] if row["id"] == a.id)
    assert fk[0]["table"] == "price_anomaly" and fk[0]["kind"] == "preexisting"
    assert fk[0]["apply_policy"] == "preserve_unchanged"


def test_proposed_create_price_anomaly_side_effect(db_session: Session) -> None:
    retailer, v = _fixture(db_session)
    _obs(db_session, retailer.id, v.id, amount="1.19", observed_at=T0)
    _obs(db_session, retailer.id, v.id, amount="1.29", observed_at=T0)  # conflict -> 2 disputed
    lane = _lane0(db_session)
    se = lane["proposed_side_effects"]
    assert len(se) == 2
    for s in se:
        assert s["type"] == "create_price_anomaly"
        assert s["anomaly_type"] == "same_timestamp_conflict"
        assert s["original_state"] == "absent"
        assert s["restore_action"] == "delete_only_created_row"
        assert "deterministic_action_id" in s and "target_observation_ref" in s


# --------------------------------------------------------------------------- #
# §4 plan_hash seals every relevant input
# --------------------------------------------------------------------------- #
def test_plan_hash_stable_across_runs(db_session: Session) -> None:
    retailer, v = _fixture(db_session)
    _obs(db_session, retailer.id, v.id, amount="1.19", observed_at=T0)
    _obs(db_session, retailer.id, v.id, amount="1.19", observed_at=T0)
    assert _plan(db_session)["report"]["plan_hash"] == _plan(db_session)["report"]["plan_hash"]


def test_plan_hash_sensitivity(db_session: Session) -> None:
    retailer, v = _fixture(db_session)
    a = _obs(db_session, retailer.id, v.id, amount="1.19", observed_at=T0)
    b = _obs(db_session, retailer.id, v.id, amount="1.19", observed_at=T0)
    canonical_id = min(a.id, b.id)  # lower id is canonical
    h0 = _plan(db_session)["report"]["plan_hash"]
    # (1) add an occurrence to the CANONICAL row (canonical unchanged) -> hash changes.
    _occ(db_session, canonical_id, crawl=_run(db_session, retailer.id))
    h1 = _plan(db_session)["report"]["plan_hash"]
    assert h1 != h0
    # (2) add a PriceAnomaly -> hash changes.
    db_session.add(PriceAnomaly(price_observation_id=a.id, anomaly_type="stale", severity="low"))
    db_session.flush()
    h2 = _plan(db_session)["report"]["plan_hash"]
    assert h2 != h1
    # (3) add a PromotionRule -> hash changes.
    db_session.add(PromotionRule(price_observation_id=a.id, type="percentage"))
    db_session.flush()
    h3 = _plan(db_session)["report"]["plan_hash"]
    assert h3 != h2
    # (4) change baseline (an unrelated observation in another lane) -> hash changes.
    _obs(db_session, retailer.id, v.id, amount="9.99", observed_at=T2, valid_until=None)
    h4 = _plan(db_session)["report"]["plan_hash"]
    assert h4 != h3


# --------------------------------------------------------------------------- #
# §5 template hash naming
# --------------------------------------------------------------------------- #
def test_rollback_row_uses_template_not_real_hash(db_session: Session) -> None:
    retailer, v = _fixture(db_session)
    _obs(db_session, retailer.id, v.id, amount="1.19", observed_at=T0)
    _obs(db_session, retailer.id, v.id, amount="1.19", observed_at=T0)
    rolled = next(row for row in _lane0(db_session)["rows"]
                  if row["action"] == "logical_rollback_exact_duplicate")
    # The template still carries the UNBOUND marker (no real post-apply hash is asserted).
    assert rolled["expected_state_template"]["rolled_back_at"] == "<remediation_run_ts>"
    assert "expected_template_hash" in rolled and "expected_hash" not in rolled


# --------------------------------------------------------------------------- #
# §6 commit provenance + §7 record_price_fact reuse blocker
# --------------------------------------------------------------------------- #
def test_apply_not_ready_without_commit_provenance(db_session: Session, monkeypatch) -> None:
    monkeypatch.delenv("PLANNER_COMMIT_SHA", raising=False)
    monkeypatch.delenv("DATABASE_CODE_SHA", raising=False)
    monkeypatch.delenv("RAILWAY_GIT_COMMIT_SHA", raising=False)
    monkeypatch.delenv("BASE_MAIN_SHA", raising=False)
    retailer, v = _fixture(db_session)
    _obs(db_session, retailer.id, v.id, amount="1.19", observed_at=T0)
    _obs(db_session, retailer.id, v.id, amount="1.19", observed_at=T0)
    r = _plan(db_session)["report"]
    assert r["apply_ready"] is False
    assert "unknown_commit_provenance" in r["apply_blockers"]
    assert r["planner_commit_sha"] == "unknown"


def test_record_price_fact_may_reuse_rolled_back_and_blocker_reported(db_session: Session) -> None:
    retailer, v = _fixture(db_session)
    rb = _obs(db_session, retailer.id, v.id, amount="1.19", observed_at=T0)  # lower id
    rb.rolled_back_at = T0  # rolled back exact duplicate
    db_session.flush()
    _obs(db_session, retailer.id, v.id, amount="1.19", observed_at=T0)  # active canonical
    candidate = PriceObservation(
        retailer_id=retailer.id, product_variant_id=v.id, price_scope="national",
        price_type="regular", amount=Decimal("1.19"), currency="EUR", observed_at=T0,
        imported_at=T0, valid_from=T0, confidence_score=Decimal("1.0"), staging_only=True)
    res = record_price_fact(db_session, candidate, OccurrenceProvenance(provider_code="x"),
                            imported_at=T0)
    # Demonstrate which row the CURRENT writer reuses (it does NOT filter rolled-back).
    reused_rolled_back = res.observation.rolled_back_at is not None
    r = _plan(db_session)["report"]
    assert r["apply_ready"] is False
    assert "record_price_fact_may_reuse_rolled_back_exact_duplicate" in r["apply_blockers"]
    # The blocker exists precisely because the writer can reuse a rolled-back row:
    assert reused_rolled_back or res.observation.id == rb.id


# --------------------------------------------------------------------------- #
# §8 excluded lanes stay in the manifest; scoped counts
# --------------------------------------------------------------------------- #
def test_excluded_lane_is_in_manifest_with_full_state(db_session: Session) -> None:
    retailer, v = _fixture(db_session)
    _obs(db_session, retailer.id, v.id, amount="1.19", observed_at=T0, status="human_verified")
    _obs(db_session, retailer.id, v.id, amount="1.29", observed_at=T0)  # conflict -> excluded
    manifest = _plan(db_session)["manifest"]
    excluded = [lane for lane in manifest["lanes"] if lane["excluded"]]
    assert len(excluded) == 1
    lane = excluded[0]
    assert lane["apply_allowed"] is False
    assert lane["proposed_side_effects"] == []
    assert all(set(row["original_values"]) == set(ident.all_columns()) for row in lane["rows"])


def test_scoped_counts_distinguish_scanned_vs_planned(db_session: Session) -> None:
    retailer, v = _fixture(db_session)
    a = _obs(db_session, retailer.id, v.id, amount="1.19", observed_at=T0)
    _obs(db_session, retailer.id, v.id, amount="1.19", observed_at=T0)
    _occ(db_session, a.id)  # ambiguous occurrence
    r = _plan(db_session)["report"]
    assert "occurrences_scanned_total" in r and "occurrences_in_planned_lanes" in r
    assert "ambiguous_provenance_scanned" in r
    assert r["occurrences_scanned_total"] >= r["occurrences_in_planned_lanes"]


def test_manifest_enables_exact_restore(db_session: Session) -> None:
    retailer, v = _fixture(db_session)
    o1 = _obs(db_session, retailer.id, v.id, amount="1.19", observed_at=T0)
    _obs(db_session, retailer.id, v.id, amount="1.19", observed_at=T0)
    row = next(r for r in _lane0(db_session)["rows"] if r["id"] == o1.id)
    assert set(row["original_values"]) == set(ident.all_columns())
    assert row["original_hash"] == ident.row_hash(ident.row_values(o1))


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
