"""API tests for the async plan flow: generate (202) -> worker -> read plan + grocery."""

from __future__ import annotations

import uuid
from datetime import date, timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from cestaplan_api.db import get_db
from cestaplan_api.models import Equipment, GenerationJob, Household
from cestaplan_worker.processor import process_job

from .conftest import csrf, login, register


def _email() -> str:
    return f"plan-{uuid.uuid4().hex[:12]}@example.com"


def _plans_client(db_session: Session) -> TestClient:
    from cestaplan_api.routers import auth, grocery, households, plans

    app = FastAPI()
    for module in (auth, households, plans, grocery):
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


def _add_equipment(db_session: Session, household_public_id: str) -> None:
    household = db_session.execute(
        select(Household).where(Household.public_id == uuid.UUID(household_public_id))
    ).scalar_one()
    for code in ("toaster", "stovetop", "blender", "oven"):
        db_session.add(
            Equipment(household_id=household.id, equipment_code=code, available=True)
        )
    db_session.flush()


def test_generate_flow_end_to_end(db_session: Session) -> None:
    client = _plans_client(db_session)
    email = _email()
    register(client, email)
    token = login(client, email)

    hh = client.post("/api/v1/households", json={"name": "Casa"}, headers=csrf(token)).json()
    # Second eater with a hard gluten allergy.
    client.post(
        f"/api/v1/households/{hh['id']}/members",
        json={
            "display_name": "Sam",
            "allergies": [{"allergen_code": "gluten", "severity": "allergy"}],
        },
        headers=csrf(token),
    )
    _add_equipment(db_session, hh["id"])

    start = date.today()
    end = start + timedelta(days=6)
    resp = client.post(
        "/api/v1/plans/generate",
        json={
            "household_id": hh["id"],
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "budget_amount": "500",
            "requirements": [
                {"meal_type": "breakfast", "requested_count": 2, "default_servings": 2},
                {"meal_type": "lunch", "requested_count": 4, "default_servings": 2},
                {"meal_type": "snack", "requested_count": 1, "default_servings": 2},
                {"meal_type": "dinner", "requested_count": 3, "default_servings": 2},
            ],
        },
        headers=csrf(token),
    )
    assert resp.status_code == 202, resp.text
    body = resp.json()
    run_id = body["optimization_run_id"]
    meal_plan_id = body["meal_plan_id"]
    assert body["status_url"] == f"/api/v1/plans/runs/{run_id}"

    # Status is queued before the worker runs.
    status_resp = client.get(body["status_url"])
    assert status_resp.status_code == 200
    assert status_resp.json()["status"] == "queued"

    # Run the worker on the enqueued job (in-process, same session).
    job = db_session.execute(
        select(GenerationJob).order_by(GenerationJob.id.desc())
    ).scalars().first()
    assert job is not None
    process_job(job, db_session)

    assert client.get(body["status_url"]).json()["status"] == "completed"

    plan = client.get(f"/api/v1/plans/{meal_plan_id}").json()
    assert plan["status"] == "ready"
    assert len(plan["planned_meals"]) == 10
    assert plan["coverage"]["status"]
    assert plan["budget_diff"] is not None

    grocery = client.get(f"/api/v1/plans/{meal_plan_id}/grocery-list").json()
    assert grocery["categories"]
    # Toggle the first item bought.
    first_item = grocery["categories"][0]["items"][0]
    toggled = client.post(
        f"/api/v1/plans/{meal_plan_id}/grocery-list/items/{first_item['id']}/toggle",
        headers=csrf(token),
    )
    assert toggled.status_code == 200
    assert toggled.json()["is_checked"] is True


def test_get_plan_requires_membership(db_session: Session) -> None:
    client = _plans_client(db_session)
    # Owner creates + generates a plan.
    owner_email = _email()
    register(client, owner_email)
    owner_token = login(client, owner_email)
    hh = client.post("/api/v1/households", json={"name": "Casa"}, headers=csrf(owner_token)).json()
    _add_equipment(db_session, hh["id"])
    start = date.today()
    gen = client.post(
        "/api/v1/plans/generate",
        json={
            "household_id": hh["id"],
            "start_date": start.isoformat(),
            "end_date": (start + timedelta(days=3)).isoformat(),
            "budget_amount": "300",
            "requirements": [
                {"meal_type": "lunch", "requested_count": 2, "default_servings": 2}
            ],
        },
        headers=csrf(owner_token),
    ).json()

    # A different user must not read the plan (404, no existence disclosure).
    other_email = _email()
    register(client, other_email)
    login(client, other_email)
    resp = client.get(f"/api/v1/plans/{gen['meal_plan_id']}")
    assert resp.status_code == 404


def test_generate_requires_csrf(db_session: Session) -> None:
    client = _plans_client(db_session)
    email = _email()
    register(client, email)
    login(client, email)
    hh = _create_household_no_csrf_guard(client, email, db_session)
    resp = client.post(
        "/api/v1/plans/generate",
        json={
            "household_id": hh,
            "start_date": date.today().isoformat(),
            "end_date": date.today().isoformat(),
            "budget_amount": "100",
            "requirements": [
                {"meal_type": "lunch", "requested_count": 1, "default_servings": 1}
            ],
        },
    )
    assert resp.status_code == 403


def _create_household_no_csrf_guard(client, email, db_session) -> str:
    token = login(client, email)
    hh = client.post("/api/v1/households", json={"name": "Casa"}, headers=csrf(token)).json()
    return hh["id"]
