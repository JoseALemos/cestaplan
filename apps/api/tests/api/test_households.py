"""Household CRUD and permission tests (roles, IDOR, membership)."""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from cestaplan_api.models import Household, HouseholdMember

from .conftest import csrf, login, register


def _email() -> str:
    return f"user-{uuid.uuid4().hex[:12]}@example.com"


def _create_household(client: TestClient, token: str, name: str = "Casa") -> dict:
    resp = client.post(
        "/api/v1/households", json={"name": name}, headers=csrf(token)
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_create_and_list_household(client: TestClient) -> None:
    email = _email()
    register(client, email)
    token = login(client, email)

    hh = _create_household(client, token, "Mi hogar")
    assert hh["my_role"] == "owner"
    assert hh["member_count"] == 1

    listing = client.get("/api/v1/households")
    assert listing.status_code == 200
    ids = [h["id"] for h in listing.json()]
    assert hh["id"] in ids


def test_create_requires_csrf(client: TestClient) -> None:
    email = _email()
    register(client, email)
    login(client, email)
    resp = client.post("/api/v1/households", json={"name": "SinCSRF"})
    assert resp.status_code == 403


def test_owner_can_add_member_with_profile(client: TestClient) -> None:
    email = _email()
    register(client, email)
    token = login(client, email)
    hh = _create_household(client, token)

    payload = {
        "display_name": "Alex",
        "diet_type": "high_protein",
        "allergies": [{"allergen_code": "peanut", "severity": "anaphylaxis"}],
        "intolerances": ["lactose"],
        "preferences": [{"subject_ref": "chicken", "sentiment": "like"}],
        "rejected_ingredients": ["liver"],
        "nutrition_goal": {"protein_target_g": "140", "energy_target_kcal": "2200"},
    }
    resp = client.post(
        f"/api/v1/households/{hh['id']}/members", json=payload, headers=csrf(token)
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["display_name"] == "Alex"
    profile = body["profile"]
    assert profile["diet_type"] == "high_protein"
    assert profile["protein_target_g"] == "140"  # Decimal serialised as string
    codes = {a["allergen_code"]: a["severity"] for a in profile["allergies"]}
    assert codes["peanut"] == "anaphylaxis"
    assert codes["lactose"] == "intolerance"
    sentiments = {p["subject_ref"]: p["sentiment"] for p in profile["preferences"]}
    assert sentiments["chicken"] == "like"
    assert sentiments["liver"] == "avoid"

    members = client.get(f"/api/v1/households/{hh['id']}/members")
    assert members.status_code == 200
    assert len(members.json()) == 2  # owner + Alex


def test_viewer_cannot_mutate(client: TestClient, db_session: Session) -> None:
    owner_email = _email()
    register(client, owner_email)
    owner_token = login(client, owner_email)
    hh = _create_household(client, owner_token)

    # A second user is added directly as a viewer member of the same household.
    viewer_email = _email()
    viewer_client_setup = register(client, viewer_email)
    household = db_session.execute(
        select(Household).where(Household.public_id == uuid.UUID(hh["id"]))
    ).scalar_one()
    from cestaplan_api.models import User

    viewer_user = db_session.execute(
        select(User).where(User.email == viewer_email)
    ).scalar_one()
    db_session.add(
        HouseholdMember(
            household_id=household.id,
            user_id=viewer_user.id,
            role="viewer",
            display_name="Viewer",
            is_eater=True,
            joined_at=household.created_at,
        )
    )
    db_session.commit()
    assert viewer_client_setup["email"] == viewer_email

    # Log in as the viewer on a separate client session.
    from fastapi.testclient import TestClient as _TC

    viewer_client = _TC(client.app)
    v_token = login(viewer_client, viewer_email)

    # Viewer can read...
    assert viewer_client.get(f"/api/v1/households/{hh['id']}").status_code == 200
    # ...but cannot add members (owner-only).
    add = viewer_client.post(
        f"/api/v1/households/{hh['id']}/members",
        json={"display_name": "Nope"},
        headers=csrf(v_token),
    )
    assert add.status_code == 403


def test_non_member_gets_404(client: TestClient) -> None:
    owner_email = _email()
    register(client, owner_email)
    owner_token = login(client, owner_email)
    hh = _create_household(client, owner_token)

    from fastapi.testclient import TestClient as _TC

    other_client = _TC(client.app)
    other_email = _email()
    register(other_client, other_email)
    other_token = login(other_client, other_email)

    # Non-member: existence is not disclosed -> 404 on read and on mutate.
    assert other_client.get(f"/api/v1/households/{hh['id']}").status_code == 404
    add = other_client.post(
        f"/api/v1/households/{hh['id']}/members",
        json={"display_name": "X"},
        headers=csrf(other_token),
    )
    assert add.status_code == 404


def test_unknown_household_404(client: TestClient) -> None:
    email = _email()
    register(client, email)
    login(client, email)
    assert client.get(f"/api/v1/households/{uuid.uuid4()}").status_code == 404


def test_update_member_replaces_collections(client: TestClient) -> None:
    email = _email()
    register(client, email)
    token = login(client, email)
    hh = _create_household(client, token)

    created = client.post(
        f"/api/v1/households/{hh['id']}/members",
        json={"display_name": "Sam", "intolerances": ["gluten"]},
        headers=csrf(token),
    ).json()
    member_id = created["id"]

    updated = client.patch(
        f"/api/v1/households/{hh['id']}/members/{member_id}",
        json={"display_name": "Samuel", "intolerances": [], "allergies": []},
        headers=csrf(token),
    )
    assert updated.status_code == 200
    body = updated.json()
    assert body["display_name"] == "Samuel"
    assert body["profile"]["allergies"] == []
