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
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event, func, select, text
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
    # The shared db_session fixture already holds pending writes and is not a fresh read-only
    # snapshot, so classification tests call the private in-snapshot body directly (spec §5 — the
    # snapshot gate itself is exercised separately with committed, independent sessions).
    return planner._dry_run_in_snapshot(db, PROVIDER)


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
# §4 sensitive KEY policy — a sensitive key is a violation regardless of its value's type
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("obj", [
    {"api_key": "abc123"},
    {"PASSWORD": "neutral"},
    {"headers": {"X-Key": "abc"}},
    {"payload": {"safe": True}},
    {"token": None},              # None value
    {"secret": ""},               # empty string
    {"access_token": 12345},      # integer
    {"client_secret": {"nested": {"refresh_token": "x"}}},
    {"outer": [{"authorization": "Bearer x"}]},  # sensitive key inside a list
    {"database_url": "postgresql://u:p@h/db"},
    {"note": "Bearer abc.def.ghi"},              # sensitive VALUE, non-sensitive key
    {"url": "https://user:pass@host/x"},         # URL with credentials (value hit)
])
def test_scanner_flags_sensitive_keys_and_values(obj) -> None:
    assert planner.scan_sensitive(obj)


def test_scanner_key_hit_is_independent_of_value_type() -> None:
    hits = planner.scan_sensitive({"api_key": 0, "password": None, "amount": "1.19"})
    kinds = {h["kind"] for h in hits}
    assert kinds == {"key"} and len(hits) == 2  # amount is not flagged


def test_scanner_accepts_url_without_credentials_but_flags_the_key() -> None:
    # A plain https URL VALUE is a value hit; but a non-sensitive key holding a bare host is clean.
    assert planner.scan_sensitive({"host": "example.com"}) == []
    assert planner.scan_sensitive({"link": "https://example.com/page"})  # value hit (http scheme)


def test_normalization_matches_hyphen_and_case_variants() -> None:
    for key in ("API-Key", "api_key", "APIKEY", "Access-Token", "raw_payload", "Raw-Payload"):
        assert planner.scan_sensitive({key: "x"}), key


def test_manifest_carries_no_sensitive_key_or_value(db_session: Session) -> None:
    retailer, v = _fixture(db_session)
    secret = "https://evil.example.com/x?token=SUPERSECRET123&api_key=abc"
    a = _obs(db_session, retailer.id, v.id, amount="1.19", observed_at=T0, source_url=secret)
    _obs(db_session, retailer.id, v.id, amount="1.19", observed_at=T0)
    _occ(db_session, a.id, crawl=_crawl(db_session, retailer.id), source_url=secret)
    res = _plan(db_session)
    dumped = json.dumps(res["manifest"], default=str)
    assert "SUPERSECRET123" not in dumped and "evil.example.com" not in dumped
    # sensitive_key_hits == 0 proves no raw `source_url` key survives (only *_present / *_hash).
    assert res["report"]["sensitive_key_hits"] == 0
    assert res["report"]["sensitive_value_hits"] == 0
    assert res["report"]["output_sensitive_scan_passed"] is True


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
            res = planner.dry_run(s, slug)
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


def _obs_id_for(rid) -> int:
    s = _isession()
    try:
        val = s.scalar(select(PriceObservation.id).where(
            PriceObservation.retailer_id == rid).limit(1))
        assert val is not None
        return int(val)
    finally:
        s.close()


def test_snapshot_full_view_is_stable_under_concurrent_write() -> None:  # §7
    slug, rid, vid = _seed_committed()
    oid = _obs_id_for(rid)
    # An UNKNOWN FK table pre-created & committed BEFORE the planner's snapshot (zero rows yet).
    setup = _isession()
    try:
        setup.execute(text("CREATE TABLE snap_unknown_ref (id bigint PRIMARY KEY, "
                           "obs_id bigint REFERENCES price_observation(id))"))
        setup.commit()
    finally:
        setup.close()
    try:
        s1 = _isession()
        try:
            snap = planner.readonly_preflight(s1)
            r1 = planner._dry_run_in_snapshot(s1, slug, snap)["report"]

            # A concurrent writer adds an observation, an occurrence, a SUPPORTED FK row, and an
            # UNKNOWN FK row — all committed AFTER the planner's snapshot was pinned.
            w = _isession()
            try:
                w.add(PriceObservation(
                    retailer_id=rid, product_variant_id=vid, price_scope="national",
                    price_type="regular", amount=Decimal("1.19"), currency="EUR", observed_at=T0,
                    imported_at=T0, valid_from=T0, confidence_score=Decimal("1.0"),
                    staging_only=True))
                w.add(PriceObservationOccurrence(
                    price_observation_id=oid, provider_code="p", imported_at=T0))
                w.add(PromotionRule(price_observation_id=oid, type="percentage"))
                w.execute(text("INSERT INTO snap_unknown_ref (id, obs_id) VALUES (1, :o)"),
                          {"o": oid})
                w.commit()
            finally:
                w.close()

            r1b = planner._dry_run_in_snapshot(s1, slug, snap)["report"]
            # The planner still sees the ENTIRE pre-write state: baseline, rows, occurrences,
            # dependency inventory, exclusions and plan_hash are all unchanged.
            for k in ("plan_hash", "lanes_scanned", "lanes_plannable", "lanes_excluded",
                      "occurrences_scanned_total", "fk_dependencies_scanned",
                      "facts_to_logically_rollback"):
                assert r1b[k] == r1[k], k
            assert r1b["lanes_excluded"] == 0
            s1.rollback()
        finally:
            s1.close()

        # After rollback, a FRESH snapshot DOES see the concurrent changes.
        s3 = _isession()
        try:
            r3 = planner.dry_run(s3, slug)["report"]
            assert r3["lanes_excluded"] == 1               # the unknown FK row is now visible
            assert r3["fk_dependencies_scanned"] >= 1       # the promotion_rule is now visible
            assert r3["plan_hash"] != r1["plan_hash"]
            s3.rollback()
        finally:
            s3.close()
    finally:
        drop = _isession()
        try:
            drop.execute(text("DROP TABLE IF EXISTS snap_unknown_ref"))
            drop.execute(text("DELETE FROM promotion_rule WHERE price_observation_id = :o"),
                         {"o": oid})
            drop.execute(text("DELETE FROM price_observation_occurrence "
                             "WHERE price_observation_id = :o"), {"o": oid})
            drop.commit()
        finally:
            drop.close()
        _cleanup(rid)


# --------------------------------------------------------------------------- #
# §6 typed safety-gate exceptions (never `assert`; hold under python -O)
# --------------------------------------------------------------------------- #
class _FakeResult:
    def __init__(self, v):
        self._v = v

    def scalar(self):
        return self._v


class _FakeSession:
    """Minimal stand-in to drive readonly_preflight's SHOW checks deterministically."""

    def __init__(self, read_only="on", isolation="repeatable read"):
        self.new = self.dirty = self.deleted = ()
        self.bind = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))
        self._ro, self._iso = read_only, isolation

    def execute(self, clause):
        sql = str(clause).lower()
        if "transaction_read_only" in sql:
            return _FakeResult(self._ro)
        if "transaction_isolation" in sql:
            return _FakeResult(self._iso)
        return _FakeResult(None)


def test_preflight_rejects_new_objects() -> None:
    s = _isession()
    try:
        s.add(Retailer(slug="dirty-new", name="x", adapter_key="test", is_synthetic=True))
        with pytest.raises(planner.PlannerSessionNotClean):
            planner.readonly_preflight(s)
    finally:
        s.rollback()
        s.close()


def test_preflight_rejects_dirty_and_deleted() -> None:
    _slug, rid, _vid = _seed_committed()
    try:
        for mutate in ("dirty", "deleted"):
            s = _isession()
            try:
                r = s.scalar(select(Retailer).where(Retailer.id == rid))
                assert r is not None
                if mutate == "dirty":
                    r.name = "mutated"
                else:
                    s.delete(r)
                with pytest.raises(planner.PlannerSessionNotClean):
                    planner.readonly_preflight(s)
            finally:
                s.rollback()
                s.close()
    finally:
        _cleanup(rid)


def test_preflight_requires_postgres() -> None:
    eng = create_engine("sqlite://")
    s = Session(bind=eng)
    try:
        with pytest.raises(planner.PlannerRequiresPostgres):
            planner.readonly_preflight(s)
    finally:
        s.close()


def test_preflight_rejects_already_started_transaction() -> None:
    s = _isession()
    try:
        s.execute(text("SELECT 1"))  # a query already ran -> SET TRANSACTION must fail
        with pytest.raises(planner.PlannerTransactionAlreadyStarted):
            planner.readonly_preflight(s)
    finally:
        s.rollback()
        s.close()


def test_preflight_rejects_non_read_only() -> None:
    with pytest.raises(planner.PlannerReadOnlySnapshotFailed):
        planner.readonly_preflight(_FakeSession(read_only="off"))  # type: ignore[arg-type]


def test_preflight_rejects_wrong_isolation() -> None:
    with pytest.raises(planner.PlannerReadOnlySnapshotFailed):
        planner.readonly_preflight(_FakeSession(isolation="serializable"))  # type: ignore[arg-type]


def test_snapshot_dry_run_is_deterministic() -> None:
    slug, rid, _vid = _seed_committed()
    try:
        s1 = _isession()
        s2 = _isession()
        try:
            h1 = planner.dry_run(s1, slug)["report"]["plan_hash"]
            h2 = planner.dry_run(s2, slug)["report"]["plan_hash"]
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
               apply_policy="preserve_unchanged", fk_schema="public", fk_constraint="c1"):
    return {
        "lane_fingerprint": "L1", "excluded": False, "exclusion_reasons": [],
        "rows": [{
            "integrity": {"full_row_hash": "h1"}, "action": "reconstruct_interval",
            "classification": "sequential_unique",
            "expected_state_template": {"valid_from": "T0", "valid_until": None},
            "expected_template_hash": "th1",
            "occurrences": [{"occurrence_hash": "o1"}],
            "incoming_fk_state": [{
                "referencing_schema": fk_schema, "referencing_table": "promotion_rule",
                "referencing_column": "price_observation_id", "referred_schema": "public",
                "referred_table": "price_observation", "referred_column": "id",
                "constraint_name": fk_constraint, "full_row_hash": "f1",
                "apply_policy": apply_policy, "restore_policy": "preserve_unchanged"}],
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


def test_seal_changes_with_fk_schema() -> None:  # §5.9 — schema is part of the sealed FK identity
    assert _seal_of(_mini_lane(fk_schema="public")) != _seal_of(_mini_lane(fk_schema="audit"))


def test_seal_changes_with_fk_constraint_name() -> None:  # §5.9
    assert _seal_of(_mini_lane(fk_constraint="c1")) != _seal_of(_mini_lane(fk_constraint="c2"))


# --------------------------------------------------------------------------- #
# §1 manifest versioning (v4)
# --------------------------------------------------------------------------- #
def test_manifest_declares_v4(db_session: Session) -> None:
    retailer, v = _fixture(db_session)
    _obs(db_session, retailer.id, v.id, amount="1.19", observed_at=T0)
    _obs(db_session, retailer.id, v.id, amount="1.19", observed_at=T0)
    m = _plan(db_session)["manifest"]
    assert m["schema_version"] == 4
    assert m["tool_version"] == "0.4.0-plan-only"
    assert planner.SCHEMA_VERSION == 4 and planner.TOOL_VERSION == "0.4.0-plan-only"


def test_plan_hash_depends_on_schema_version(monkeypatch) -> None:
    base = _seal_of(_mini_lane())
    monkeypatch.setattr(planner, "SCHEMA_VERSION", 999)
    assert _seal_of(_mini_lane()) != base


def test_plan_hash_depends_on_tool_version(monkeypatch) -> None:
    base = _seal_of(_mini_lane())
    monkeypatch.setattr(planner, "TOOL_VERSION", "9.9.9-other")
    assert _seal_of(_mini_lane()) != base


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
    # §4: an early exclusion could not classify the rows -> explicit preflight diagnostic.
    for row in lane["rows"]:
        assert row["diagnostic_classification"] == "unclassified_null_timestamp"


# --------------------------------------------------------------------------- #
# §4 excluded lanes preserve the pre-exclusion diagnostic (or an explicit preflight marker)
# --------------------------------------------------------------------------- #
def test_excluded_human_conflict_preserves_diagnostic(db_session: Session) -> None:  # §4.1
    retailer, v = _fixture(db_session)
    _obs(db_session, retailer.id, v.id, amount="1.19", observed_at=T0, status="human_verified")
    _obs(db_session, retailer.id, v.id, amount="1.29", observed_at=T0)
    lane = next(x for x in _plan(db_session)["manifest"]["lanes"] if x["excluded"])
    _assert_excluded_clean(lane)
    diags = {row["diagnostic_classification"] for row in lane["rows"]}
    assert "same_timestamp_semantic_conflict_representative" in diags  # conflict diagnosis kept
    assert "excluded" not in diags


def test_excluded_lane_with_dups_keeps_dup_diagnostic(db_session: Session) -> None:  # §4.2
    retailer, v = _fixture(db_session)
    _obs(db_session, retailer.id, v.id, amount="1.19", observed_at=T0)
    _obs(db_session, retailer.id, v.id, amount="1.19", observed_at=T0)  # exact duplicate of A
    _obs(db_session, retailer.id, v.id, amount="1.29", observed_at=T0,
         status="human_verified")  # forces a human-reviewed conflict -> late exclusion
    lane = next(x for x in _plan(db_session)["manifest"]["lanes"] if x["excluded"])
    _assert_excluded_clean(lane)
    diags = [row["diagnostic_classification"] for row in lane["rows"]]
    assert "exact_duplicate_noncanonical" in diags  # the duplicate is still identifiable
    assert lane["planned_changes"] == 0 and lane["proposed_side_effects"] == []


def test_excluded_unknown_fk_uses_preflight_diagnostic(db_session: Session) -> None:  # §4.3
    retailer, v = _fixture(db_session)
    a = _obs(db_session, retailer.id, v.id, amount="1.19", observed_at=T0)
    _obs(db_session, retailer.id, v.id, amount="1.19", observed_at=T0)
    db_session.execute(text("CREATE TABLE synth_ref2 (id bigint PRIMARY KEY, "
                            "obs_id bigint REFERENCES price_observation(id))"))
    db_session.execute(text("INSERT INTO synth_ref2 (id, obs_id) VALUES (1, :o)"), {"o": a.id})
    db_session.flush()
    lane = next(x for x in _plan(db_session)["manifest"]["lanes"] if x["excluded"])
    _assert_excluded_clean(lane)
    for row in lane["rows"]:
        assert row["diagnostic_classification"] == "unclassified_unknown_fk"
        assert row["action"] == "excluded_no_action"


# --------------------------------------------------------------------------- #
# §5 FK discovery — referencing vs referred schema, full-key handlers, composite-safe, multi-schema
# --------------------------------------------------------------------------- #
def _fk_for(db, table):
    return [f for f in planner.discover_incoming_fks(db) if f["referencing_table"] == table]


def test_all_model_fks_have_exact_handlers() -> None:
    # Every model FK to price_observation.id is covered by an EXACT (schema, table, column) handler.
    assert planner.metadata_fk_keys() <= set(planner._FK_HANDLERS)


def test_public_fk_records_both_schemas(db_session: Session) -> None:  # §5.1
    retailer, v = _fixture(db_session)
    _obs(db_session, retailer.id, v.id, amount="1.19", observed_at=T0)
    db_session.execute(text("CREATE TABLE pub_ref (id bigint PRIMARY KEY, "
                            "obs_id bigint REFERENCES price_observation(id))"))
    db_session.flush()
    fk = _fk_for(db_session, "pub_ref")[0]
    assert fk["referencing_schema"] == "public" and fk["referred_schema"] == "public"
    assert fk["referencing_column"] == "obs_id" and fk["referred_column"] == "id"
    assert fk["referred_table"] == "price_observation" and fk["supported"] is False


def test_audit_schema_fk_keeps_ref_sides_distinct(db_session: Session) -> None:  # §5.2
    retailer, v = _fixture(db_session)
    _obs(db_session, retailer.id, v.id, amount="1.19", observed_at=T0)
    db_session.execute(text("CREATE SCHEMA IF NOT EXISTS audit"))
    db_session.execute(text(
        "CREATE TABLE audit.legacy_reference (id bigint PRIMARY KEY, "
        "price_observation_id bigint REFERENCES public.price_observation(id))"))
    db_session.flush()
    fk = _fk_for(db_session, "legacy_reference")[0]
    assert fk["referencing_schema"] == "audit"
    assert fk["referencing_table"] == "legacy_reference"
    assert fk["referencing_column"] == "price_observation_id"
    assert fk["referred_schema"] == "public"
    assert fk["referred_table"] == "price_observation"
    assert fk["referred_column"] == "id"
    assert fk["constraint_name"] and fk["supported"] is False  # audit.* not in the public registry


def test_same_name_in_other_schema_is_unknown(db_session: Session) -> None:  # §5.3
    retailer, v = _fixture(db_session)
    a = _obs(db_session, retailer.id, v.id, amount="1.19", observed_at=T0)
    _obs(db_session, retailer.id, v.id, amount="1.19", observed_at=T0)
    db_session.execute(text("CREATE SCHEMA IF NOT EXISTS audit"))
    db_session.execute(text(
        "CREATE TABLE audit.price_anomaly (id bigint PRIMARY KEY, "
        "price_observation_id bigint REFERENCES public.price_observation(id))"))
    db_session.execute(text(
        "INSERT INTO audit.price_anomaly (id, price_observation_id) VALUES (1, :o)"), {"o": a.id})
    db_session.flush()
    audit_fk = next(f for f in _fk_for(db_session, "price_anomaly")
                    if f["referencing_schema"] == "audit")
    assert audit_fk["supported"] is False  # NOT auto-supported by sharing the table name
    r = _plan(db_session)["report"]
    assert "audit.price_anomaly.price_observation_id" in r["fk_unknown"]
    assert r["lanes_excluded"] == 1


def test_composite_fk_only_pairs_the_id_column(db_session: Session) -> None:  # §5.4
    retailer, v = _fixture(db_session)
    a = _obs(db_session, retailer.id, v.id, amount="1.19", observed_at=T0)
    _obs(db_session, retailer.id, v.id, amount="1.19", observed_at=T0)
    db_session.execute(text(
        "CREATE TABLE multi_ref (k int PRIMARY KEY, "
        "obs_id bigint REFERENCES price_observation(id), ret_id bigint REFERENCES retailer(id))"))
    db_session.execute(text("INSERT INTO multi_ref (k, obs_id, ret_id) VALUES (1, :o, :r)"),
                       {"o": a.id, "r": retailer.id})
    db_session.flush()
    cols = [f["referencing_column"] for f in _fk_for(db_session, "multi_ref")]
    assert cols == ["obs_id"]  # ret_id (-> retailer) is NOT recorded
    assert _plan(db_session)["report"]["lanes_excluded"] == 1


def test_quoted_schema_and_table_reflect_correctly(db_session: Session) -> None:  # §5.5
    retailer, v = _fixture(db_session)
    a = _obs(db_session, retailer.id, v.id, amount="1.19", observed_at=T0)
    _obs(db_session, retailer.id, v.id, amount="1.19", observed_at=T0)
    db_session.execute(text('CREATE SCHEMA IF NOT EXISTS "Weird Schema"'))
    db_session.execute(text('CREATE TABLE "Weird Schema"."Odd Ref" (id bigint PRIMARY KEY, '
                            "obs_id bigint REFERENCES public.price_observation(id))"))
    db_session.execute(text('INSERT INTO "Weird Schema"."Odd Ref" (id, obs_id) VALUES (1, :o)'),
                       {"o": a.id})
    db_session.flush()
    r = _plan(db_session)["report"]
    assert any("Weird Schema" in u and "Odd Ref" in u for u in r["fk_unknown"])
    assert r["lanes_excluded"] == 1


def test_known_fk_without_rows_does_not_exclude(db_session: Session) -> None:  # §5.6
    retailer, v = _fixture(db_session)
    _obs(db_session, retailer.id, v.id, amount="1.19", observed_at=T0)
    _obs(db_session, retailer.id, v.id, amount="1.19", observed_at=T0)
    r = _plan(db_session)["report"]
    assert r["lanes_excluded"] == 0
    # The supported FKs are discovered even with zero referencing rows.
    assert "public.price_anomaly.price_observation_id" in r["fk_supported"]


def test_unknown_fk_with_row_excludes_only_its_lane(db_session: Session) -> None:  # §5.7
    retailer, v = _fixture(db_session)
    a = _obs(db_session, retailer.id, v.id, amount="1.19", observed_at=T0)
    _obs(db_session, retailer.id, v.id, amount="1.19", observed_at=T0)
    _p, v2 = seed_test_catalog_product(db_session, retailer, "PL-2", name="Plan2", price=None)
    _obs(db_session, retailer.id, v2.id, amount="2.19", observed_at=T0)  # a second, CLEAN lane
    _obs(db_session, retailer.id, v2.id, amount="2.19", observed_at=T0)
    db_session.execute(text("CREATE TABLE synth_ref (id bigint PRIMARY KEY, "
                            "obs_id bigint REFERENCES price_observation(id))"))
    db_session.execute(text("INSERT INTO synth_ref (id, obs_id) VALUES (1, :o)"), {"o": a.id})
    db_session.flush()
    r = _plan(db_session)["report"]
    assert r["lanes_excluded"] == 1 and r["lanes_plannable"] == 1  # only a's lane excluded


def test_unknown_fk_without_rows_inventoried_only(db_session: Session) -> None:  # §5.8
    retailer, v = _fixture(db_session)
    _obs(db_session, retailer.id, v.id, amount="1.19", observed_at=T0)
    _obs(db_session, retailer.id, v.id, amount="1.19", observed_at=T0)
    db_session.execute(text("CREATE TABLE empty_ref (id bigint PRIMARY KEY, "
                            "obs_id bigint REFERENCES price_observation(id))"))
    db_session.flush()
    r = _plan(db_session)["report"]
    assert "public.empty_ref.obs_id" in r["fk_unknown"]  # inventoried
    assert r["lanes_excluded"] == 0  # but no referencing rows -> excludes nothing


def test_reflection_never_queries_the_wrong_schema(db_session: Session) -> None:  # §5.10
    # The referencing row lives in audit.legacy_reference; no public table of that name exists. A
    # correct exclusion via the audit ref proves reflection used schema=audit (never public).
    retailer, v = _fixture(db_session)
    a = _obs(db_session, retailer.id, v.id, amount="1.19", observed_at=T0)
    _obs(db_session, retailer.id, v.id, amount="1.19", observed_at=T0)
    db_session.execute(text("CREATE SCHEMA IF NOT EXISTS audit"))
    db_session.execute(text(
        "CREATE TABLE audit.legacy_reference (id bigint PRIMARY KEY, "
        "price_observation_id bigint REFERENCES public.price_observation(id))"))
    db_session.execute(
        text("INSERT INTO audit.legacy_reference (id, price_observation_id) VALUES (1, :o)"),
        {"o": a.id})
    db_session.flush()
    lane = next(x for x in _plan(db_session)["manifest"]["lanes"] if x["excluded"])
    assert any(
        "audit.legacy_reference.price_observation_id->public.price_observation.id" in reason
        for reason in lane["exclusion_reasons"])


# --------------------------------------------------------------------------- #
# §2/§3 support requires BOTH sides; a homonym in another schema is foreign, never a dependency
# --------------------------------------------------------------------------- #
def _fkdict(*, rsch="public", rtab="price_anomaly", rcol="price_observation_id",
           dsch="public", dtab="price_observation", dcol="id", name="c"):
    return {"referencing_schema": rsch, "referencing_table": rtab, "referencing_column": rcol,
            "referred_schema": dsch, "referred_table": dtab, "referred_column": dcol,
            "constraint_name": name}


def test_support_requires_both_sides() -> None:  # §3.1 / §3.2 / §3.3
    assert planner._fk_supported(_fkdict())                   # public.price_anomaly -> public PK
    assert not planner._fk_supported(_fkdict(dsch="audit"))   # -> audit.price_observation.id
    assert not planner._fk_supported(_fkdict(rsch="audit"))   # audit.price_anomaly -> unknown


def test_classification_domain_vs_foreign() -> None:
    assert planner._fk_classification(_fkdict()) == "domain_supported"
    assert planner._fk_classification(_fkdict(rtab="synth")) == "domain_unknown"
    assert planner._fk_classification(_fkdict(dsch="audit")) == "foreign_homonym"


def test_homonym_referred_is_foreign_never_a_dependency(db_session: Session) -> None:  # §3.2/§3.4
    retailer, v = _fixture(db_session)
    a = _obs(db_session, retailer.id, v.id, amount="1.19", observed_at=T0)
    _obs(db_session, retailer.id, v.id, amount="1.19", observed_at=T0)
    base = _plan(db_session)["report"]["plan_hash"]
    db_session.execute(text("CREATE SCHEMA IF NOT EXISTS audit"))
    db_session.execute(text("CREATE TABLE audit.price_observation (id bigint PRIMARY KEY)"))
    db_session.execute(text("INSERT INTO audit.price_observation (id) VALUES (:o)"), {"o": a.id})
    db_session.execute(text(
        "CREATE TABLE pa_like (id bigint PRIMARY KEY, "
        "price_observation_id bigint REFERENCES audit.price_observation(id))"))
    db_session.execute(text("INSERT INTO pa_like (id, price_observation_id) VALUES (1, :o)"),
                       {"o": a.id})
    db_session.flush()
    r = _plan(db_session)["report"]
    # An FK to audit.price_observation is foreign — reported, but NOT supported/unknown, and it
    # never excludes a lane by an accidental ID match. Its presence still moves the plan_hash.
    assert any("audit.price_observation.id" in f for f in r["fk_foreign_ignored"])
    assert not any("pa_like" in f for f in r["fk_supported"])
    assert not any("pa_like" in f for f in r["fk_unknown"])
    assert r["lanes_excluded"] == 0
    assert r["plan_hash"] != base


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
        result = planner._dry_run_in_snapshot(db_session, PROVIDER)
    writes = [s for s in stmts if s.lstrip()[:6].upper() in ("INSERT", "UPDATE", "DELETE")]
    assert writes == []
    actions = {row["action"] for lane in result["manifest"]["lanes"] for row in lane["rows"]}
    assert "delete" not in " ".join(actions).lower()
    after = int(db_session.scalar(select(func.count()).select_from(PriceObservation).where(
        PriceObservation.retailer_id == retailer.id)) or 0)
    assert after == before
