"""Admin mapping review queue — HTTP gate tests (spec §1/§3).

Proves that a COMPETING candidate (active=false) is still visible and approvable from admin, and
that the queue is admin-only. Never touches production.
"""

from __future__ import annotations

from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from cestaplan_api.models import Ingredient, ProviderIngredientMapping
from tests.admin.conftest import csrf, login, promote_to_admin, register

_BASE = "/api/v1/admin/ingredient-product-mappings"


def _competing(db: Session, key: str, ext: str) -> ProviderIngredientMapping:
    ing = db.execute(select(Ingredient).where(Ingredient.canonical_name == key)).scalar_one()
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
        params={"provider_code": "parsebot-alcampo", "canonical_ingredient_key": "tomate",
                "relation_status": "competing", "limit": 200},
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
