"""Pantry (despensa) CRUD, role/membership enforcement and string-money tests.

The pantry router is wired here on the test app (main.py wiring is exercised in prod);
membership/roles come from the shared conftest helpers. A final test confirms that a
household's pantry rows are exactly what ``planning_context`` reads, i.e. the ingredient
the household has in stock is the one the planner will buy less of.
"""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from cestaplan_api.models import Household, HouseholdMember, PantryItem, User

from .conftest import csrf, login, register


def _email() -> str:
    return f"user-{uuid.uuid4().hex[:12]}@example.com"


def _create_household(client: TestClient, token: str, name: str = "Casa") -> dict:
    resp = client.post("/api/v1/households", json={"name": name}, headers=csrf(token))
    assert resp.status_code == 201, resp.text
    return resp.json()


def _add_item(client: TestClient, token: str, hid: str, **body):
    payload = {"name": "tomate", "quantity": "2.5", "unit": "kg"}
    payload.update(body)
    return client.post(
        f"/api/v1/households/{hid}/pantry", json=payload, headers=csrf(token)
    )


# --------------------------------------------------------------------------- #
# Happy paths
# --------------------------------------------------------------------------- #
def test_add_list_update_delete(client: TestClient) -> None:
    email = _email()
    register(client, email)
    token = login(client, email)
    hh = _create_household(client, token)
    hid = hh["id"]

    # Add
    resp = _add_item(client, token, hid, expires_at="2026-09-01")
    assert resp.status_code == 201, resp.text
    item = resp.json()
    assert item["canonical_name"] == "tomate"
    assert item["display"] == "Tomate"
    assert item["quantity"] == "2.5000"  # Decimal(12,4) serialised as string
    assert item["unit"] == "kg"
    assert item["expires_at"] == "2026-09-01"
    item_id = item["id"]

    # List
    listing = client.get(f"/api/v1/households/{hid}/pantry")
    assert listing.status_code == 200
    assert [i["id"] for i in listing.json()] == [item_id]

    # Update quantity + clear caducidad
    upd = client.patch(
        f"/api/v1/households/{hid}/pantry/{item_id}",
        json={"quantity": "5", "expires_at": None},
        headers=csrf(token),
    )
    assert upd.status_code == 200, upd.text
    assert upd.json()["quantity"] == "5.0000"
    assert upd.json()["expires_at"] is None

    # Delete
    dele = client.delete(
        f"/api/v1/households/{hid}/pantry/{item_id}", headers=csrf(token)
    )
    assert dele.status_code == 204
    assert client.get(f"/api/v1/households/{hid}/pantry").json() == []


def test_add_by_display_name_case_insensitive(client: TestClient) -> None:
    email = _email()
    register(client, email)
    token = login(client, email)
    hh = _create_household(client, token)
    resp = _add_item(client, token, hh["id"], name="TOMATE")
    assert resp.status_code == 201, resp.text
    assert resp.json()["canonical_name"] == "tomate"


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #
def test_quantity_must_be_positive(client: TestClient) -> None:
    email = _email()
    register(client, email)
    token = login(client, email)
    hh = _create_household(client, token)
    resp = _add_item(client, token, hh["id"], quantity="0")
    assert resp.status_code == 422


def test_unknown_unit_rejected(client: TestClient) -> None:
    email = _email()
    register(client, email)
    token = login(client, email)
    hh = _create_household(client, token)
    resp = _add_item(client, token, hh["id"], unit="handfuls")
    assert resp.status_code == 422


def test_unknown_ingredient_rejected(client: TestClient) -> None:
    email = _email()
    register(client, email)
    token = login(client, email)
    hh = _create_household(client, token)
    resp = _add_item(client, token, hh["id"], name="dragonfruit-nope")
    assert resp.status_code == 422


def test_add_requires_csrf(client: TestClient) -> None:
    email = _email()
    register(client, email)
    login(client, email)
    hh = _create_household(client, login(client, email))
    resp = client.post(
        f"/api/v1/households/{hh['id']}/pantry",
        json={"name": "tomate", "quantity": "1", "unit": "kg"},
    )
    assert resp.status_code == 403


# --------------------------------------------------------------------------- #
# Membership / roles (IDOR-safe)
# --------------------------------------------------------------------------- #
def test_viewer_cannot_mutate(client: TestClient, db_session: Session) -> None:
    owner_email = _email()
    register(client, owner_email)
    owner_token = login(client, owner_email)
    hh = _create_household(client, owner_token)

    viewer_email = _email()
    register(client, viewer_email)
    household = db_session.execute(
        select(Household).where(Household.public_id == uuid.UUID(hh["id"]))
    ).scalar_one()
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

    viewer_client = TestClient(client.app)
    v_token = login(viewer_client, viewer_email)

    # Viewer can read...
    assert viewer_client.get(f"/api/v1/households/{hh['id']}/pantry").status_code == 200
    # ...but cannot add stock (editor+).
    add = _add_item(viewer_client, v_token, hh["id"])
    assert add.status_code == 403


def test_non_member_gets_404(client: TestClient) -> None:
    owner_email = _email()
    register(client, owner_email)
    owner_token = login(client, owner_email)
    hh = _create_household(client, owner_token)

    other_client = TestClient(client.app)
    other_email = _email()
    register(other_client, other_email)
    other_token = login(other_client, other_email)

    # Existence not disclosed -> 404 on read and on mutate.
    assert other_client.get(f"/api/v1/households/{hh['id']}/pantry").status_code == 404
    add = _add_item(other_client, other_token, hh["id"])
    assert add.status_code == 404


def test_item_from_other_household_not_found(client: TestClient) -> None:
    email = _email()
    register(client, email)
    token = login(client, email)
    hh_a = _create_household(client, token, "A")
    hh_b = _create_household(client, token, "B")
    item = _add_item(client, token, hh_a["id"]).json()

    # Same owner, but the item does not belong to household B -> 404 (scoped lookup).
    resp = client.patch(
        f"/api/v1/households/{hh_b['id']}/pantry/{item['id']}",
        json={"quantity": "9"},
        headers=csrf(token),
    )
    assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# Planner integration (read-only assertion on what planning_context reads)
# --------------------------------------------------------------------------- #
def test_pantry_rows_feed_planning_context(client: TestClient, db_session: Session) -> None:
    """A stocked ingredient is present as a planner-visible DTO, so the plan buys less.

    We do not touch generation; we assert the exact rows ``planning_context._build_pantry``
    consumes (ingredient_id set + not deleted), which is the contract that makes the
    deterministic ``PantryCalculator`` subtract this stock from the shopping list.
    """
    from cestaplan_api.services.planning_context import _build_pantry

    email = _email()
    register(client, email)
    token = login(client, email)
    hh = _create_household(client, token)
    _add_item(client, token, hh["id"], name="tomate", quantity="3", unit="kg")

    household = db_session.execute(
        select(Household).where(Household.public_id == uuid.UUID(hh["id"]))
    ).scalar_one()

    rows = db_session.execute(
        select(PantryItem).where(PantryItem.household_id == household.id)
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].ingredient_id is not None
    assert rows[0].deleted_at is None

    dtos = _build_pantry(db_session, household.id)
    stocked = {dto.canonical_name: dto for dto in dtos}
    assert "tomate" in stocked
    assert stocked["tomate"].unit == "kg"
    assert str(stocked["tomate"].quantity) == "3.0000"
