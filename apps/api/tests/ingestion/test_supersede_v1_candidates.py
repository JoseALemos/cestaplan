"""A.2 supersede v1 -> v2 (spec §8): the v2 product-first discovery supersedes stale v1 candidate
mappings — setting superseded_at, superseded_reason='candidate_quality_v2' and active=False while
preserving the machine proposal (proposed_*/evidence_json) and NEVER overwriting a human decision
(reviewed_at/reviewed_by). Idempotent."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from cestaplan_api.services.mapping_review import supersede_v1_candidates
from tests.fixtures.provider_scenarios import (
    ensure_test_ingredient,
    seed_test_mapping_candidate,
    seed_test_retailer,
)

PROVIDER = "test_supersede_provider"
NOW = datetime(2026, 7, 25, 12, 0, tzinfo=UTC)


def _v1_candidate(db: Session, ext: str, **kw):
    retailer = seed_test_retailer(db, PROVIDER)
    ing = ensure_test_ingredient(db, f"ingr_{ext}")
    return seed_test_mapping_candidate(
        db, PROVIDER, ing, ext, retailer_slug=retailer.slug, **kw
    )


def test_supersedes_unreviewed_v1_candidate(db_session: Session) -> None:
    row = _v1_candidate(db_session, "SUP-1", evidence_json={"product_name": "Leche", "k": "v"})
    assert row.mapping_version == "1.0.0" and row.superseded_at is None

    r = supersede_v1_candidates(db_session, PROVIDER, now=NOW)
    assert r["superseded_v1_candidates"] == 1
    db_session.refresh(row)
    assert row.superseded_at == NOW
    assert row.superseded_reason == "candidate_quality_v2"
    assert row.active is False
    # The machine proposal / evidence is preserved, and no human review is fabricated.
    assert row.evidence_json == {"product_name": "Leche", "k": "v"}
    assert row.reviewed_at is None and row.reviewed_by is None


def test_is_idempotent(db_session: Session) -> None:
    _v1_candidate(db_session, "SUP-2")
    supersede_v1_candidates(db_session, PROVIDER, now=NOW)
    r2 = supersede_v1_candidates(db_session, PROVIDER, now=NOW)
    assert r2["superseded_v1_candidates"] == 0  # nothing left to supersede


def test_reviewed_candidate_is_never_superseded(db_session: Session) -> None:
    row = _v1_candidate(db_session, "SUP-3")
    row.reviewed_at = NOW  # a human decision exists
    db_session.flush()
    r = supersede_v1_candidates(db_session, PROVIDER, now=NOW)
    assert r["superseded_v1_candidates"] == 0
    db_session.refresh(row)
    assert row.superseded_at is None  # decision preserved


def test_v2_candidate_is_not_superseded(db_session: Session) -> None:
    row = _v1_candidate(db_session, "SUP-4")
    row.mapping_version = "2.0.0"
    db_session.flush()
    r = supersede_v1_candidates(db_session, PROVIDER, now=NOW)
    assert r["superseded_v1_candidates"] == 0
    db_session.refresh(row)
    assert row.superseded_at is None
