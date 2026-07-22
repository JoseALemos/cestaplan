"""Household invitation flow: invite (owner), accept (matching email), revoke, expiry.

Covers the security invariants: only the owner may invite/revoke, an invitation can
never grant the owner role, acceptance requires an email match, a revoked or expired
invitation cannot be accepted, and only the token's hash is persisted (the raw token is
returned exactly once).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from cestaplan_api.models import Household, HouseholdInvitation, HouseholdMember, User
from cestaplan_api.security import hash_token

from .conftest import csrf, login, register


def _email() -> str:
    return f"user-{uuid.uuid4().hex[:12]}@example.com"


def _create_household(client: TestClient, token: str, name: str = "Casa") -> dict:
    resp = client.post("/api/v1/households", json={"name": name}, headers=csrf(token))
    assert resp.status_code == 201, resp.text
    return resp.json()


def _new_client(app) -> TestClient:
    return TestClient(app)


def _invite(client: TestClient, token: str, household_id: str, email: str, role: str) -> dict:
    resp = client.post(
        f"/api/v1/households/{household_id}/invitations",
        json={"email": email, "role": role},
        headers=csrf(token),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_invite_then_accept_creates_member_with_role(
    client: TestClient, db_session: Session
) -> None:
    owner_email = _email()
    register(client, owner_email)
    owner_token = login(client, owner_email)
    hh = _create_household(client, owner_token)

    invitee_email = _email()
    invitee_client = _new_client(client.app)
    register(invitee_client, invitee_email)
    invitee_token = login(invitee_client, invitee_email)

    created = _invite(client, owner_token, hh["id"], invitee_email, "editor")
    assert created["invitation"]["email"] == invitee_email
    assert created["invitation"]["role"] == "editor"
    assert created["invitation"]["status"] == "pending"
    token = created["token"]
    assert token
    assert created["accept_path"] == f"/invitaciones/{token}"

    accepted = invitee_client.post(
        f"/api/v1/invitations/{token}/accept", headers=csrf(invitee_token)
    )
    assert accepted.status_code == 200, accepted.text
    body = accepted.json()
    assert body["household_id"] == hh["id"]
    assert body["role"] == "editor"

    invitee_user = db_session.execute(
        select(User).where(User.email == invitee_email)
    ).scalar_one()
    household = db_session.execute(
        select(Household).where(Household.public_id == uuid.UUID(hh["id"]))
    ).scalar_one()
    member = db_session.execute(
        select(HouseholdMember).where(
            HouseholdMember.household_id == household.id,
            HouseholdMember.user_id == invitee_user.id,
        )
    ).scalar_one()
    assert member.role == "editor"
    assert member.is_eater is True

    invitation = db_session.execute(
        select(HouseholdInvitation).where(
            HouseholdInvitation.household_id == household.id
        )
    ).scalar_one()
    assert invitation.status == "accepted"
    assert invitation.accepted_user_id == invitee_user.id


def test_token_is_hashed_and_not_leaked(client: TestClient, db_session: Session) -> None:
    owner_email = _email()
    register(client, owner_email)
    owner_token = login(client, owner_email)
    hh = _create_household(client, owner_token)

    invitee_email = _email()
    created = _invite(client, owner_token, hh["id"], invitee_email, "viewer")
    token = created["token"]

    invitation = db_session.execute(
        select(HouseholdInvitation).where(HouseholdInvitation.email == invitee_email)
    ).scalar_one()
    # Only the hash is stored; the raw token never lands in the database.
    assert invitation.token_hash == hash_token(token)
    assert token.encode("utf-8") != invitation.token_hash

    # The list endpoint exposes no token (raw or hashed).
    listing = client.get(f"/api/v1/households/{hh['id']}/invitations")
    assert listing.status_code == 200
    rows = listing.json()
    assert len(rows) == 1
    assert "token" not in rows[0]
    assert "token_hash" not in rows[0]
    assert rows[0]["email"] == invitee_email


def test_non_owner_cannot_invite(client: TestClient, db_session: Session) -> None:
    owner_email = _email()
    register(client, owner_email)
    owner_token = login(client, owner_email)
    hh = _create_household(client, owner_token)

    editor_email = _email()
    register(client, editor_email)
    household = db_session.execute(
        select(Household).where(Household.public_id == uuid.UUID(hh["id"]))
    ).scalar_one()
    editor_user = db_session.execute(
        select(User).where(User.email == editor_email)
    ).scalar_one()
    db_session.add(
        HouseholdMember(
            household_id=household.id,
            user_id=editor_user.id,
            role="editor",
            display_name="Editor",
            is_eater=True,
            joined_at=household.created_at,
        )
    )
    db_session.commit()

    editor_client = _new_client(client.app)
    editor_token = login(editor_client, editor_email)
    resp = editor_client.post(
        f"/api/v1/households/{hh['id']}/invitations",
        json={"email": _email(), "role": "viewer"},
        headers=csrf(editor_token),
    )
    assert resp.status_code == 403


def test_invitation_role_owner_rejected(client: TestClient) -> None:
    owner_email = _email()
    register(client, owner_email)
    owner_token = login(client, owner_email)
    hh = _create_household(client, owner_token)

    resp = client.post(
        f"/api/v1/households/{hh['id']}/invitations",
        json={"email": _email(), "role": "owner"},
        headers=csrf(owner_token),
    )
    assert resp.status_code == 422


def test_wrong_email_cannot_accept(client: TestClient) -> None:
    owner_email = _email()
    register(client, owner_email)
    owner_token = login(client, owner_email)
    hh = _create_household(client, owner_token)

    invited_email = _email()
    created = _invite(client, owner_token, hh["id"], invited_email, "viewer")
    token = created["token"]

    # A different account (email does not match the invitation) may not accept it.
    other_email = _email()
    other_client = _new_client(client.app)
    register(other_client, other_email)
    other_token = login(other_client, other_email)
    resp = other_client.post(
        f"/api/v1/invitations/{token}/accept", headers=csrf(other_token)
    )
    assert resp.status_code == 403


def test_revoke_prevents_accept(client: TestClient) -> None:
    owner_email = _email()
    register(client, owner_email)
    owner_token = login(client, owner_email)
    hh = _create_household(client, owner_token)

    invitee_email = _email()
    invitee_client = _new_client(client.app)
    register(invitee_client, invitee_email)
    invitee_token = login(invitee_client, invitee_email)

    created = _invite(client, owner_token, hh["id"], invitee_email, "viewer")
    token = created["token"]
    invitation_id = created["invitation"]["id"]

    revoke = client.delete(
        f"/api/v1/households/{hh['id']}/invitations/{invitation_id}",
        headers=csrf(owner_token),
    )
    assert revoke.status_code == 204

    resp = invitee_client.post(
        f"/api/v1/invitations/{token}/accept", headers=csrf(invitee_token)
    )
    assert resp.status_code == 409


def test_expired_invitation_rejected(client: TestClient, db_session: Session) -> None:
    owner_email = _email()
    register(client, owner_email)
    owner_token = login(client, owner_email)
    hh = _create_household(client, owner_token)

    invitee_email = _email()
    invitee_client = _new_client(client.app)
    register(invitee_client, invitee_email)
    invitee_token = login(invitee_client, invitee_email)

    created = _invite(client, owner_token, hh["id"], invitee_email, "viewer")
    token = created["token"]

    invitation = db_session.execute(
        select(HouseholdInvitation).where(HouseholdInvitation.email == invitee_email)
    ).scalar_one()
    invitation.expires_at = datetime.now(UTC) - timedelta(days=1)
    db_session.flush()

    resp = invitee_client.post(
        f"/api/v1/invitations/{token}/accept", headers=csrf(invitee_token)
    )
    assert resp.status_code == 410


def test_preview_reports_email_match(client: TestClient) -> None:
    owner_email = _email()
    register(client, owner_email)
    owner_token = login(client, owner_email)
    hh = _create_household(client, owner_token, "Casa Azul")

    invitee_email = _email()
    invitee_client = _new_client(client.app)
    register(invitee_client, invitee_email)
    login(invitee_client, invitee_email)

    created = _invite(client, owner_token, hh["id"], invitee_email, "editor")
    token = created["token"]

    # The invited account sees a match; a preview never carries the token.
    preview = invitee_client.get(f"/api/v1/invitations/{token}")
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert body["household_name"] == "Casa Azul"
    assert body["role"] == "editor"
    assert body["status"] == "pending"
    assert body["email_matches"] is True
    assert "token" not in body

    # A different logged-in account sees email_matches=False.
    other_email = _email()
    other_client = _new_client(client.app)
    register(other_client, other_email)
    login(other_client, other_email)
    preview2 = other_client.get(f"/api/v1/invitations/{token}")
    assert preview2.status_code == 200
    assert preview2.json()["email_matches"] is False

    # Unauthenticated preview is rejected (drives the /login redirect on the front).
    anon = _new_client(client.app)
    assert anon.get(f"/api/v1/invitations/{token}").status_code == 401


def test_duplicate_pending_invitation_rejected(client: TestClient) -> None:
    owner_email = _email()
    register(client, owner_email)
    owner_token = login(client, owner_email)
    hh = _create_household(client, owner_token)

    invitee_email = _email()
    _invite(client, owner_token, hh["id"], invitee_email, "viewer")
    dup = client.post(
        f"/api/v1/households/{hh['id']}/invitations",
        json={"email": invitee_email, "role": "editor"},
        headers=csrf(owner_token),
    )
    assert dup.status_code == 409
