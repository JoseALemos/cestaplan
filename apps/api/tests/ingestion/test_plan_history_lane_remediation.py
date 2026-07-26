"""Read-only history-lane remediation PLANNER (design phase). Proves: correct classification, a
stable canonical policy, anchor reconstruction, a reversible manifest, an in-memory simulator that
holds every invariant, idempotence, and — critically — that the dry-run executes ONLY SELECTs (no
INSERT/UPDATE/DELETE) and proposes zero deletions."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import event, func, select
from sqlalchemy.orm import Session

from cestaplan_api.models import (
    CrawlRun,
    PriceAnomaly,
    PriceObservation,
    PriceObservationOccurrence,
    PromotionRule,
)
from cestaplan_api.services import observation_identity as ident
from cestaplan_api.tools import plan_history_lane_remediation as planner
from tests.fixtures.provider_scenarios import (
    seed_test_catalog_product,
    seed_test_retailer,
)

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
        imported_at=imp or observed_at,
        valid_from=observed_at, valid_until=valid_until, confidence_score=Decimal("1.0"),
        staging_only=True, verification_status=status,
    )
    db.add(o)
    db.flush()
    return o


def _occ(db, obs_id, *, crawl=None, source=None, provider="p"):
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


@contextmanager
def _capture_sql(db: Session):
    """Record every statement executed on the session's connection while inside the block."""
    stmts: list[str] = []
    conn = db.connection()

    def _before(conn, cursor, statement, params, context, executemany):
        stmts.append(statement)

    event.listen(conn.engine, "before_cursor_execute", _before)
    try:
        yield stmts
    finally:
        event.remove(conn.engine, "before_cursor_execute", _before)


def _writes(stmts):
    return [s for s in stmts if s.lstrip()[:6].upper() in ("INSERT", "UPDATE", "DELETE")]


# 1. Two exact duplicates with distinct provenance -> 1 canonical (richer provenance), 1 rollback.
def test_two_exact_duplicates_distinct_provenance(db_session: Session) -> None:
    retailer, v = _fixture(db_session)
    a = _obs(db_session, retailer.id, v.id, amount="1.19", observed_at=T0)  # poor provenance
    b = _obs(db_session, retailer.id, v.id, amount="1.19", observed_at=T0)  # richer provenance
    _occ(db_session, b.id, crawl=_run(db_session, retailer.id))  # b has demonstrable provenance
    r = _plan(db_session)["report"]
    assert r["exact_duplicate_groups"] == 1
    assert r["facts_to_logically_rollback"] == 1
    # b (richer provenance) is canonical -> a is the one rolled back.
    lane = _plan(db_session)["manifest"]["lanes"][0]
    rolled = [row["id"] for row in lane["rows"]
              if row["action"] == "logical_rollback_exact_duplicate"]
    assert rolled == [a.id]


# 2. Six exact duplicates open (the historical max) -> 1 canonical, 5 rollbacks, <=1 open post-sim.
def test_six_exact_duplicates(db_session: Session) -> None:
    retailer, v = _fixture(db_session)
    for _ in range(6):
        _obs(db_session, retailer.id, v.id, amount="1.19", observed_at=T0, valid_until=None)
    r = _plan(db_session)["report"]
    assert r["facts_to_logically_rollback"] == 5
    assert r["projected_invariants_all_ok"] is True
    assert r["lanes_plannable"] == 1


# 3. Two distinct prices at the same timestamp -> a semantic conflict, both disputed.
def test_same_timestamp_two_prices(db_session: Session) -> None:
    retailer, v = _fixture(db_session)
    _obs(db_session, retailer.id, v.id, amount="1.19", observed_at=T0)
    _obs(db_session, retailer.id, v.id, amount="1.29", observed_at=T0)
    r = _plan(db_session)["report"]
    assert r["semantic_conflict_groups"] == 1
    assert r["facts_to_mark_disputed"] == 2


# 4. Three distinct conflicts at the same timestamp -> all three disputed.
def test_same_timestamp_three_way(db_session: Session) -> None:
    retailer, v = _fixture(db_session)
    for amount in ("1.19", "1.29", "1.39"):
        _obs(db_session, retailer.id, v.id, amount=amount, observed_at=T0)
    assert _plan(db_session)["report"]["facts_to_mark_disputed"] == 3


# 5. Exact duplicates plus a semantic conflict -> dups roll back, conflict disputed.
def test_exact_dups_plus_conflict(db_session: Session) -> None:
    retailer, v = _fixture(db_session)
    _obs(db_session, retailer.id, v.id, amount="1.19", observed_at=T0)  # exact dup pair at T0...
    _obs(db_session, retailer.id, v.id, amount="1.19", observed_at=T0)
    _obs(db_session, retailer.id, v.id, amount="2.00", observed_at=T1)  # ...and a conflict at T1
    _obs(db_session, retailer.id, v.id, amount="2.50", observed_at=T1)
    r = _plan(db_session)["report"]
    assert r["facts_to_logically_rollback"] == 1  # one of the T0 dups
    assert r["facts_to_mark_disputed"] == 2  # the two T1 conflict facts


# 6. Several open rows at T0/T1/T2 -> reconstructed into a coherent chain (invariants hold).
def test_multiple_open_reconstructed(db_session: Session) -> None:
    retailer, v = _fixture(db_session)
    for t, a in ((T0, "1.00"), (T1, "1.10"), (T2, "1.20")):
        _obs(db_session, retailer.id, v.id, amount=a, observed_at=t, valid_until=None)  # all open
    r = _plan(db_session)["report"]
    assert r["intervals_to_reconstruct"] >= 1
    assert r["projected_invariants_all_ok"] is True


# 7. Out-of-order stored rows still reconstruct correctly.
def test_out_of_order_reconstructed(db_session: Session) -> None:
    retailer, v = _fixture(db_session)
    _obs(db_session, retailer.id, v.id, amount="1.20", observed_at=T2, valid_until=None)
    _obs(db_session, retailer.id, v.id, amount="1.00", observed_at=T0, valid_until=None)
    _obs(db_session, retailer.id, v.id, amount="1.10", observed_at=T1, valid_until=None)
    lane = _plan(db_session)["manifest"]["lanes"][0]
    exp = {row["expected_state"]["valid_from"]: row["expected_state"]["valid_until"]
           for row in lane["rows"]}
    assert exp[T0] == T1 and exp[T1] == T2 and exp[T2] is None


# 8. A conflict barrier with a later observation -> the later fact is open, barrier not crossed.
def test_conflict_barrier_then_later(db_session: Session) -> None:
    retailer, v = _fixture(db_session)
    _obs(db_session, retailer.id, v.id, amount="1.19", observed_at=T0)  # conflict at T0
    _obs(db_session, retailer.id, v.id, amount="1.29", observed_at=T0)
    _obs(db_session, retailer.id, v.id, amount="2.00", observed_at=T1, valid_until=None)  # later
    r = _plan(db_session)["report"]
    assert r["facts_to_mark_disputed"] == 2
    assert r["projected_invariants_all_ok"] is True


# 9. An ambiguous occurrence is counted as ambiguous provenance.
def test_ambiguous_occurrence(db_session: Session) -> None:
    retailer, v = _fixture(db_session)
    a = _obs(db_session, retailer.id, v.id, amount="1.19", observed_at=T0)
    b = _obs(db_session, retailer.id, v.id, amount="1.19", observed_at=T0)
    _occ(db_session, a.id, crawl=None, source=None, provider=None)  # all-null provenance
    _occ(db_session, b.id, crawl=_run(db_session, retailer.id))
    assert _plan(db_session)["report"]["ambiguous_provenance"] >= 1


# 10. A fact with no occurrence at all is handled (counted ambiguous, planned normally).
def test_fact_without_occurrence(db_session: Session) -> None:
    retailer, v = _fixture(db_session)
    _obs(db_session, retailer.id, v.id, amount="1.19", observed_at=T0)
    _obs(db_session, retailer.id, v.id, amount="1.19", observed_at=T0)  # exact dup, no occurrences
    r = _plan(db_session)["report"]
    assert r["facts_to_logically_rollback"] == 1
    assert r["ambiguous_provenance"] >= 1


# 11. An incoming PromotionRule is preserved and does NOT block the plan.
def test_incoming_promotion_rule_preserved(db_session: Session) -> None:
    retailer, v = _fixture(db_session)
    a = _obs(db_session, retailer.id, v.id, amount="1.19", observed_at=T0)
    _obs(db_session, retailer.id, v.id, amount="1.19", observed_at=T0)
    db_session.add(PromotionRule(price_observation_id=a.id, type="percentage"))
    db_session.flush()
    r = _plan(db_session)["report"]
    assert r["lanes_excluded"] == 0
    assert r["fk_dependencies"] >= 1


# 12. An existing PriceAnomaly is preserved and does NOT block the plan.
def test_existing_price_anomaly_preserved(db_session: Session) -> None:
    retailer, v = _fixture(db_session)
    a = _obs(db_session, retailer.id, v.id, amount="1.19", observed_at=T0)
    _obs(db_session, retailer.id, v.id, amount="1.19", observed_at=T0)
    db_session.add(PriceAnomaly(price_observation_id=a.id, anomaly_type="stale", severity="low"))
    db_session.flush()
    assert _plan(db_session)["report"]["lanes_excluded"] == 0


# 13. A lane with an UNKNOWN incoming FK is excluded (simulated by narrowing the known-FK set).
def test_lane_excluded_by_unknown_fk(db_session: Session, monkeypatch) -> None:
    retailer, v = _fixture(db_session)
    a = _obs(db_session, retailer.id, v.id, amount="1.19", observed_at=T0)
    _obs(db_session, retailer.id, v.id, amount="1.19", observed_at=T0)
    db_session.add(PriceAnomaly(price_observation_id=a.id, anomaly_type="stale", severity="low"))
    db_session.flush()
    monkeypatch.setattr(planner, "_KNOWN_FK", ("promotion_rule",))  # price_anomaly now "unknown"
    r = _plan(db_session)["report"]
    assert r["lanes_excluded"] == 1
    assert any(k.startswith("uncovered_fk") for k in r["exclusion_reasons"])
    assert r["manual_review_required"] >= 1


# 14. The plan is idempotent: same data -> same plan_hash; a clean lane -> zero planned changes.
def test_idempotent_plan_hash(db_session: Session) -> None:
    retailer, v = _fixture(db_session)
    _obs(db_session, retailer.id, v.id, amount="1.19", observed_at=T0)
    _obs(db_session, retailer.id, v.id, amount="1.19", observed_at=T0)
    h1 = _plan(db_session)["report"]["plan_hash"]
    h2 = _plan(db_session)["report"]["plan_hash"]
    assert h1 == h2


def test_clean_lane_has_zero_planned_changes(db_session: Session) -> None:
    retailer, v = _fixture(db_session)
    _obs(db_session, retailer.id, v.id, amount="1.00", observed_at=T0, valid_until=T1)
    _obs(db_session, retailer.id, v.id, amount="1.10", observed_at=T1, valid_until=None)
    r = _plan(db_session)["report"]
    assert r.get("lanes_plannable", 0) == 0  # nothing to plan


def test_canonical_tiebreak_is_lowest_id(db_session: Session) -> None:
    retailer, v = _fixture(db_session)
    a = _obs(db_session, retailer.id, v.id, amount="1.19", observed_at=T0)  # identical provenance
    b = _obs(db_session, retailer.id, v.id, amount="1.19", observed_at=T0)
    lane = _plan(db_session)["manifest"]["lanes"][0]
    rolled = [row["id"] for row in lane["rows"]
              if row["action"] == "logical_rollback_exact_duplicate"]
    assert rolled == [max(a.id, b.id)]  # lower id is canonical, higher id rolled back


# 15. The manifest fully captures each row so the original state can be restored exactly.
def test_manifest_enables_exact_restore(db_session: Session) -> None:
    retailer, v = _fixture(db_session)
    o1 = _obs(db_session, retailer.id, v.id, amount="1.19", observed_at=T0)
    _obs(db_session, retailer.id, v.id, amount="1.19", observed_at=T0)
    lane = _plan(db_session)["manifest"]["lanes"][0]
    row = next(r for r in lane["rows"] if r["id"] == o1.id)
    # Original values cover every column, and the recorded hash matches the live row.
    assert set(row["original_values"]) == set(ident.all_columns())
    assert row["original_hash"] == ident.row_hash(ident.row_values(o1))


# 16 + 17. The dry-run executes ONLY SELECTs (no INSERT/UPDATE/DELETE) and proposes zero deletions.
def test_dry_run_is_select_only_and_zero_deletes(db_session: Session) -> None:
    retailer, v = _fixture(db_session)
    _obs(db_session, retailer.id, v.id, amount="1.19", observed_at=T0)
    _obs(db_session, retailer.id, v.id, amount="1.19", observed_at=T0)
    obs_before = int(db_session.scalar(
        select(func.count()).select_from(PriceObservation).where(
            PriceObservation.retailer_id == retailer.id)) or 0)
    with _capture_sql(db_session) as stmts:
        result = planner.dry_run(db_session, PROVIDER)
    assert _writes(stmts) == []  # not a single INSERT/UPDATE/DELETE
    # No proposed action ever deletes: the vocabulary is rollback/dispute/reconstruct/keep only.
    actions = {row["action"] for lane in result["manifest"]["lanes"] for row in lane["rows"]}
    assert "delete" not in " ".join(actions).lower()
    obs_after = int(db_session.scalar(
        select(func.count()).select_from(PriceObservation).where(
            PriceObservation.retailer_id == retailer.id)) or 0)
    assert obs_after == obs_before  # DB untouched
