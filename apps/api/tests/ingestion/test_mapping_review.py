"""Admin mapping review + dedup + revoke (spec §1/§5/§10) — DB-backed, no network."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from cestaplan_api.models import Ingredient, ProviderIngredientMapping, User
from cestaplan_api.services import mapping_review as mr

_NOW = datetime(2026, 7, 24, tzinfo=UTC)


def _reviewer(db: Session) -> int:
    user = User(email=f"rev-{id(db)}@x.com", password_hash="x", display_name="Rev")
    db.add(user)
    db.flush()
    return user.id


def _map(
    db: Session,
    *,
    ext: str,
    key: str = "aceite_oliva",
    status: str = "candidate",
    conf: str = "0.9",
    method: str = "exact_alias",
    review: bool = True,
    unit: str = "compatible",
    warnings: list[str] | None = None,
) -> ProviderIngredientMapping:
    ing = db.execute(select(Ingredient).where(Ingredient.canonical_name == key)).scalar_one()
    row = ProviderIngredientMapping(
        provider_code="parsebot-alcampo",
        ingredient_id=ing.id,
        canonical_ingredient_key=key,
        retailer_slug="alcampo",
        external_product_id=ext,
        mapping_status=status,
        mapping_method=method,
        confidence_score=Decimal(conf),
        unit_compatibility=unit,
        required_review=review,
        active=False,
        evidence_json={"warnings": warnings or []},
    )
    db.add(row)
    db.flush()
    return row


def _competing_pair(db: Session) -> tuple[ProviderIngredientMapping, ProviderIngredientMapping]:
    a = _map(db, ext="CMP-1", key="tomate", conf="0.8")  # same product...
    b = _map(db, ext="CMP-1", key="cebolla", conf="0.7")  # ...different ingredient (competitor)
    mr.tag_conflicts(db, now=_NOW)
    return a, b


def test_competing_candidates_are_not_deduplicated(db_session: Session) -> None:
    a, b = _competing_pair(db_session)
    res = mr.consolidate_duplicates(db_session, now=_NOW)  # only EXACT duplicates consolidate
    assert res["superseded_exact_duplicates"] == 0
    assert a.superseded_at is None and b.superseded_at is None  # both kept
    assert a.relation_status == "competing" and b.relation_status == "competing"
    assert a.conflict_group_id == b.conflict_group_id  # same conflict group


def test_approval_resolves_conflict_and_rejects_competitors(db_session: Session) -> None:
    rid = _reviewer(db_session)
    a, b = _competing_pair(db_session)
    mr.approve(db_session, a.id, reviewer_id=rid, reason="correct ingredient", now=_NOW)
    assert a.relation_status == "conflict_resolved" and a.active is True
    assert b.relation_status == "rejected_competitor" and b.active is False
    assert b.resolved_by_mapping_id == a.id  # traceable


def test_only_one_approved_active_per_product(db_session: Session) -> None:
    rid = _reviewer(db_session)
    a, b = _competing_pair(db_session)
    mr.approve(db_session, a.id, reviewer_id=rid, reason=None, now=_NOW)
    # b lost the conflict; forcing it back to a candidate and approving must be refused.
    b.mapping_status = "candidate"
    b.active = False
    b.relation_status = "competing"
    db_session.flush()
    with pytest.raises(mr.ReviewError):
        mr.approve(db_session, b.id, reviewer_id=rid, reason=None, now=_NOW)


def test_revoke_reopens_competitors(db_session: Session) -> None:
    rid = _reviewer(db_session)
    a, b = _competing_pair(db_session)
    mr.approve(db_session, a.id, reviewer_id=rid, reason=None, now=_NOW)
    assert b.relation_status == "rejected_competitor"
    mr.revoke(db_session, a.id, reviewer_id=rid, reason="was wrong", now=_NOW)
    assert a.active is False
    assert b.relation_status == "competing" and b.required_review is True  # reopened


def test_approve_records_traceability(db_session: Session) -> None:
    rid = _reviewer(db_session)
    row = _map(db_session, ext="APR-1")
    mr.approve(db_session, row.id, reviewer_id=rid, reason="looks right", now=_NOW)
    assert row.mapping_status == "manually_approved"
    assert row.active is True and row.required_review is False
    assert row.reviewed_by == rid and row.reviewed_at == _NOW
    assert (row.evidence_json or {}).get("decision") == "manually_approved"


def test_reject_requires_reason(db_session: Session) -> None:
    rid = _reviewer(db_session)
    row = _map(db_session, ext="REJ-1")
    with pytest.raises(mr.ReviewError):
        mr.reject(db_session, row.id, reviewer_id=rid, reason="")
    mr.reject(db_session, row.id, reviewer_id=rid, reason="wrong product", now=_NOW)
    assert row.mapping_status == "rejected" and row.active is False


def test_revoke_is_idempotent_and_keeps_history(db_session: Session) -> None:
    rid = _reviewer(db_session)
    row = _map(db_session, ext="REV-1")
    mr.approve(db_session, row.id, reviewer_id=rid, reason=None, now=_NOW)
    mr.revoke(db_session, row.id, reviewer_id=rid, reason="mistake", now=_NOW)
    mr.revoke(db_session, row.id, reviewer_id=rid, reason="mistake again", now=_NOW)
    assert row.active is False and row.required_review is True
    assert len((row.evidence_json or {}).get("revocations", [])) == 2  # history preserved


def test_bulk_approve_blocked_for_ambiguous(db_session: Session) -> None:
    rid = _reviewer(db_session)
    a = _map(db_session, ext="AMB-1", key="tomate", status="ambiguous", conf="0.6")
    b = _map(db_session, ext="AMB-2", key="tomate", status="ambiguous", conf="0.6")
    with pytest.raises(mr.ReviewError):
        mr.bulk_approve(db_session, [a.id, b.id], reviewer_id=rid, reason=None)


def test_bulk_approve_valid_same_ingredient(db_session: Session) -> None:
    rid = _reviewer(db_session)
    a = _map(db_session, ext="OK-1", status="candidate", conf="0.9", warnings=[])
    b = _map(db_session, ext="OK-2", status="candidate", conf="0.9", warnings=[])
    result = mr.bulk_approve(db_session, [a.id, b.id], reviewer_id=rid, reason="verified")
    assert set(result["approved"]) == {a.id, b.id}  # type: ignore[arg-type]
    assert a.active is True and b.active is True


def test_bulk_approve_requires_single_ingredient(db_session: Session) -> None:
    rid = _reviewer(db_session)
    a = _map(db_session, ext="MIX-1", key="aceite_oliva", conf="0.9")
    b = _map(db_session, ext="MIX-2", key="sal", conf="0.9")
    with pytest.raises(mr.ReviewError):
        mr.bulk_approve(db_session, [a.id, b.id], reviewer_id=rid, reason=None)


def test_audit_counts_multi_ingredient_products(db_session: Session) -> None:
    before = mr.audit(db_session, "parsebot-alcampo")
    assert "products_mapped_to_multiple_ingredients" in before
    total = before["total"]
    assert isinstance(total, int) and total >= 0


def test_candidate_metrics_are_per_provider_and_flag_explosion(db_session: Session) -> None:
    a = mr.candidate_metrics(db_session, "parsebot-alcampo")
    c = mr.candidate_metrics(db_session, "parsebot-carrefour")
    # Documented ratios present and per-provider (never mixed).
    for m in (a, c):
        assert set(m) >= {
            "candidate_pair_ratio", "multi_ingredient_product_ratio",
            "average_candidates_per_conflict_group", "explosion_state",
        }
    # Alcampo is clean; Carrefour's candidate explosion is critical and blocks auto-approval.
    assert a["explosion_state"] == "ok" and a["auto_approval_allowed"] is True
    assert c["explosion_state"] == "critical" and c["auto_approval_allowed"] is False
