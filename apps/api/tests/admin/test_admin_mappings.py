"""Admin mapping review queue — HTTP gate tests (spec §1/§3).

Proves that a COMPETING candidate (active=false) is still visible and approvable from admin, and
that the queue is admin-only. Never touches production.
"""

from __future__ import annotations

from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from cestaplan_api.models import ProviderIngredientMapping
from tests.admin.conftest import csrf, login, promote_to_admin, register
from tests.fixtures.provider_scenarios import ensure_test_ingredient

_BASE = "/api/v1/admin/ingredient-product-mappings"


def _competing(db: Session, key: str, ext: str) -> ProviderIngredientMapping:
    ing = ensure_test_ingredient(db, key)  # hermetic: create the ingredient if absent
    row = ProviderIngredientMapping(
        provider_code="parsebot-alcampo",
        ingredient_id=ing.id,
        canonical_ingredient_key=key,
        retailer_slug="alcampo",
        external_product_id=ext,
        mapping_status="candidate",
        mapping_method="exact_alias",
        confidence_score=Decimal("0.8"),
        relation_status="competing",
        conflict_group_id=f"parsebot-alcampo:{ext}",
        required_review=True,
        active=False,
        evidence_json={"product_name": "Tomate pera"},
    )
    db.add(row)
    db.flush()
    return row


def test_queue_requires_admin(client: TestClient, db_session: Session) -> None:
    register(client, "user@x.com")
    login(client, "user@x.com")
    resp = client.get(f"{_BASE}/candidates")
    assert resp.status_code == 403  # non-admin is refused


def test_competing_candidate_is_visible_and_approvable(
    client: TestClient, db_session: Session
) -> None:
    row = _competing(db_session, "tomate", "CMP-VIS-1")
    register(client, "admin@x.com")
    token = login(client, "admin@x.com")
    promote_to_admin(db_session, "admin@x.com")

    # Visible in the queue despite active=false (filtered to isolate it from ambient rows)...
    listed = client.get(
        f"{_BASE}/candidates",
        params={
            "provider_code": "parsebot-alcampo",
            "canonical_ingredient_key": "tomate",
            "relation_status": "competing",
            "limit": 200,
        },
    )
    assert listed.status_code == 200
    item = next(i for i in listed.json()["items"] if i["mapping_id"] == row.id)
    assert item["relation_status"] == "competing"
    assert item["reviewable"] is True
    assert item["selectable_for_costing"] is False  # not usable for costing until approved
    assert item["review_notice"]

    # ...and approvable from admin.
    approved = client.post(
        f"{_BASE}/{row.id}/approve", json={"reason": "correct"}, headers=csrf(token)
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["active"] is True
    db_session.refresh(row)
    assert row.mapping_status == "manually_approved"


def test_summary_carries_two_layer_price_metrics(client: TestClient, db_session: Session) -> None:
    _competing(db_session, "tomate", "SUM-1")
    register(client, "adm-sum@x.com")
    login(client, "adm-sum@x.com")
    promote_to_admin(db_session, "adm-sum@x.com")

    resp = client.get(f"{_BASE}/summary/parsebot-alcampo")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # Candidate metrics stay; the two-layer price block is nested separately (never conflated).
    assert "candidate_pair_ratio" in body
    price = body["price_observations"]
    assert "unique_price_facts" in price
    assert "provenance_occurrences" in price
    assert "confirmed repeatedly" in price["note"]
    assert price["quality_gate"]["status"] in ("ok", "warning", "critical")


def test_detail_and_reject_flow(client: TestClient, db_session: Session) -> None:
    row = _competing(db_session, "cebolla", "CMP-DET-1")
    register(client, "adm2@x.com")
    token = login(client, "adm2@x.com")
    promote_to_admin(db_session, "adm2@x.com")

    detail = client.get(f"{_BASE}/{row.id}")
    assert detail.status_code == 200 and detail.json()["lifecycle_status"] == "pending"
    # reject requires a reason
    assert client.post(f"{_BASE}/{row.id}/reject", json={}, headers=csrf(token)).status_code == 422
    ok = client.post(f"{_BASE}/{row.id}/reject", json={"reason": "wrong"}, headers=csrf(token))
    assert ok.status_code == 200 and ok.json()["mapping_status"] == "rejected"


def test_enrich_endpoint_admin_only_and_completes(
    client: TestClient, db_session: Session, monkeypatch
) -> None:
    row = _competing(db_session, "tomate", "ENR-HTTP-1")
    # Non-admin refused.
    register(client, "plainuser@x.com")
    login(client, "plainuser@x.com")
    assert client.post(f"{_BASE}/{row.id}/enrich").status_code == 403
    # Admin + monkeypatched detail fetcher (no network).
    import cestaplan_api.services.mapping_enrichment as enr

    monkeypatch.setattr(
        enr,
        "_default_detail_fetcher",
        lambda provider_code, external_product_id, settings: {"category": "verduras", "unit": "kg"},
    )
    register(client, "adm3@x.com")
    token = login(client, "adm3@x.com")
    promote_to_admin(db_session, "adm3@x.com")
    resp = client.post(f"{_BASE}/{row.id}/enrich", headers=csrf(token))
    assert resp.status_code == 200, resp.text
    assert resp.json()["enrichment_status"] == "completed"


def test_e2e_conflict_enrich_approve_revoke(
    client: TestClient, db_session: Session, monkeypatch
) -> None:
    from sqlalchemy import select as _select

    from cestaplan_api.models import ProviderIngredientMapping
    from cestaplan_api.services import mapping_review as mr

    # 1. two competing candidates for the SAME product (different ingredients).
    tom = ensure_test_ingredient(db_session, "tomate")
    ceb = ensure_test_ingredient(db_session, "cebolla")

    def _mk(ing_id: int) -> ProviderIngredientMapping:
        r = ProviderIngredientMapping(
            provider_code="parsebot-alcampo",
            ingredient_id=ing_id,
            canonical_ingredient_key="tomate",
            retailer_slug="alcampo",
            external_product_id="E2E-P",
            mapping_status="candidate",
            mapping_method="normalized_name",
            confidence_score=Decimal("0.7"),
            required_review=True,
            active=False,
            evidence_json={"product_name": "Producto E2E"},
        )
        db_session.add(r)
        db_session.flush()
        return r

    a, b = _mk(tom.id), _mk(ceb.id)
    mr.tag_conflicts(db_session)
    db_session.flush()

    import cestaplan_api.services.mapping_enrichment as enr

    monkeypatch.setattr(
        enr,
        "_default_detail_fetcher",
        lambda p, e, s: {"category": "verduras", "unit": "kg"},
    )
    register(client, "e2e@x.com")
    token = login(client, "e2e@x.com")
    promote_to_admin(db_session, "e2e@x.com")

    # 2. both visible in the conflict group.
    grp = client.get(f"{_BASE}/candidates", params={"conflict_group_id": a.conflict_group_id})
    ids = {i["mapping_id"] for i in grp.json()["items"]}
    assert {a.id, b.id} <= ids

    # 3-4. enrich + approve the correct one.
    assert (
        client.post(f"{_BASE}/{a.id}/enrich", headers=csrf(token)).json()["enrichment_status"]
        == "completed"
    )
    assert (
        client.post(
            f"{_BASE}/{a.id}/approve", json={"reason": "correct"}, headers=csrf(token)
        ).status_code
        == 200
    )
    db_session.refresh(a)
    db_session.refresh(b)
    assert a.active is True and b.relation_status == "rejected_competitor"

    # 5. revoke reopens the eligible competitor.
    assert (
        client.post(
            f"{_BASE}/{a.id}/revoke", json={"reason": "was wrong"}, headers=csrf(token)
        ).status_code
        == 200
    )
    db_session.refresh(b)
    assert a.active is False and b.relation_status == "competing"

    # 6. production is never touched by any of this.
    from cestaplan_api.models import ProviderActivation

    act = db_session.execute(
        _select(ProviderActivation).where(ProviderActivation.provider_code == "parsebot-alcampo")
    ).scalar_one_or_none()
    if act is not None:
        assert act.production_eligibility is False and act.production_approved is False
