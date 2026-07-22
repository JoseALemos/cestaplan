"""API tests for the favorites/feedback list + clear endpoints in ``routers/plans``."""

from __future__ import annotations

import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from cestaplan_api.db import get_db
from cestaplan_api.models import Recipe

from .conftest import csrf, login, register


def _email() -> str:
    return f"fav-{uuid.uuid4().hex[:12]}@example.com"


def _favorites_client(db_session: Session) -> TestClient:
    from cestaplan_api.routers import auth, households, plans

    app = FastAPI()
    for module in (auth, households, plans):
        app.include_router(module.router)

    def _override_get_db():
        try:
            yield db_session
            db_session.commit()
        except Exception:
            db_session.rollback()
            raise

    app.dependency_overrides[get_db] = _override_get_db
    return TestClient(app)


def _public_recipes(db_session: Session, count: int) -> list[Recipe]:
    recipes = db_session.execute(
        select(Recipe).where(Recipe.is_public.is_(True)).order_by(Recipe.id).limit(count)
    ).scalars().all()
    assert len(recipes) == count
    return list(recipes)


def _register_with_household(client: TestClient) -> tuple[str, str]:
    """Register + login + create a household; return (csrf_token, household_public_id)."""
    email = _email()
    register(client, email)
    token = login(client, email)
    hh = client.post("/api/v1/households", json={"name": "Casa"}, headers=csrf(token)).json()
    return token, hh["id"]


def test_list_favorites_add_and_remove(db_session: Session) -> None:
    client = _favorites_client(db_session)
    token, household_id = _register_with_household(client)
    recipe = _public_recipes(db_session, 1)[0]
    recipe_id = str(recipe.public_id)

    # Empty before favoriting.
    empty = client.get(f"/api/v1/plans/recipes/favorites?household_id={household_id}")
    assert empty.status_code == 200
    assert empty.json() == []

    add = client.post(
        f"/api/v1/plans/recipes/{recipe_id}/favorite?household_id={household_id}",
        headers=csrf(token),
    )
    assert add.status_code == 201

    listed = client.get(
        f"/api/v1/plans/recipes/favorites?household_id={household_id}"
    ).json()
    assert len(listed) == 1
    row = listed[0]
    assert row["recipe_id"] == recipe_id
    assert row["title"] == recipe.title
    assert row["meal_types"] == list(recipe.meal_types or [])
    assert row["cuisine"] == recipe.cuisine
    assert row["preparation_minutes"] == recipe.preparation_minutes
    assert row["cooking_minutes"] == recipe.cooking_minutes
    assert row["tags"] == list(recipe.preference_tags or [])
    assert row["favorited_at"]

    remove = client.delete(
        f"/api/v1/plans/recipes/{recipe_id}/favorite?household_id={household_id}",
        headers=csrf(token),
    )
    assert remove.status_code == 204

    after = client.get(
        f"/api/v1/plans/recipes/favorites?household_id={household_id}"
    ).json()
    assert after == []


def test_favorites_requires_membership(db_session: Session) -> None:
    client = _favorites_client(db_session)
    _owner_token, household_id = _register_with_household(client)

    other_email = _email()
    register(client, other_email)
    login(client, other_email)

    resp = client.get(f"/api/v1/plans/recipes/favorites?household_id={household_id}")
    assert resp.status_code == 404


def test_feedback_list_filters_by_sentiment_and_clears(db_session: Session) -> None:
    client = _favorites_client(db_session)
    token, household_id = _register_with_household(client)
    rejected_recipe, liked_recipe = _public_recipes(db_session, 2)

    reject_resp = client.post(
        f"/api/v1/plans/recipes/{rejected_recipe.public_id}/feedback?household_id={household_id}",
        json={"sentiment": "reject"},
        headers=csrf(token),
    )
    assert reject_resp.status_code == 200

    like_resp = client.post(
        f"/api/v1/plans/recipes/{liked_recipe.public_id}/feedback?household_id={household_id}",
        json={"sentiment": "like"},
        headers=csrf(token),
    )
    assert like_resp.status_code == 200

    all_feedback = client.get(
        f"/api/v1/plans/recipes/feedback?household_id={household_id}"
    ).json()
    assert {row["recipe_id"] for row in all_feedback} == {
        str(rejected_recipe.public_id),
        str(liked_recipe.public_id),
    }

    only_rejected = client.get(
        f"/api/v1/plans/recipes/feedback?household_id={household_id}&sentiment=reject"
    ).json()
    assert len(only_rejected) == 1
    assert only_rejected[0]["recipe_id"] == str(rejected_recipe.public_id)
    assert only_rejected[0]["sentiment"] == "reject"
    assert only_rejected[0]["title"] == rejected_recipe.title

    clear = client.delete(
        f"/api/v1/plans/recipes/{rejected_recipe.public_id}/feedback?household_id={household_id}",
        headers=csrf(token),
    )
    assert clear.status_code == 204

    after_clear = client.get(
        f"/api/v1/plans/recipes/feedback?household_id={household_id}&sentiment=reject"
    ).json()
    assert after_clear == []

    # Clearing an already-cleared entry is a no-op, not an error.
    clear_again = client.delete(
        f"/api/v1/plans/recipes/{rejected_recipe.public_id}/feedback?household_id={household_id}",
        headers=csrf(token),
    )
    assert clear_again.status_code == 204
