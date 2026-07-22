"""FASE 5 quotas: cloud-only, server-side generation limits + the usage summary endpoint.

Quotas are enforced ONLY when ``deployment_mode == "cloud"``; ``self_hosted`` is never
limited. Counts are derived server-side from persisted ``OptimizationRun`` rows. The
managed API key is never revealed by any response.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from typing import Any

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from cestaplan_api.config import Settings
from cestaplan_api.db import get_db
from cestaplan_api.models import Household, UsageLedger
from cestaplan_api.routers import usage as usage_router
from cestaplan_api.services import quota as quota_service
from cestaplan_api.services.quota import check_generation_quota
from tests.api.conftest import csrf, login, register
from tests.worker.factory import enqueue_plan, make_household


def _cloud_settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {"deployment_mode": "cloud"}
    base.update(overrides)
    return Settings(**base)


# --------------------------------------------------------------------------- #
# Service-level: cloud limits vs self_hosted
# --------------------------------------------------------------------------- #
def test_cloud_quota_blocks_when_generations_exceeded(db_session: Session) -> None:
    _user, household, member = make_household(db_session, allergen=None)
    enqueue_plan(db_session, household, member, budget="500")
    enqueue_plan(db_session, household, member, budget="500")  # 2 runs this period

    # Limit reached (2 >= 2) -> 429.
    with pytest.raises(HTTPException) as exc:
        check_generation_quota(
            db_session,
            household_id=household.id,
            settings=_cloud_settings(cloud_monthly_generation_limit=2),
        )
    assert exc.value.status_code == 429
    assert "límite mensual" in exc.value.detail

    # Under the limit (2 < 3) -> allowed.
    check_generation_quota(
        db_session,
        household_id=household.id,
        settings=_cloud_settings(cloud_monthly_generation_limit=3),
    )


def test_cloud_daily_quota_blocks(db_session: Session) -> None:
    _user, household, member = make_household(db_session, allergen=None)
    enqueue_plan(db_session, household, member, budget="500")
    with pytest.raises(HTTPException) as exc:
        check_generation_quota(
            db_session,
            household_id=household.id,
            settings=_cloud_settings(
                cloud_monthly_generation_limit=0, cloud_daily_generation_limit=1
            ),
        )
    assert exc.value.status_code == 429
    assert "límite diario" in exc.value.detail


def test_cloud_token_quota_blocks(db_session: Session) -> None:
    _user, household, _member = make_household(db_session, allergen=None)
    db_session.add(
        UsageLedger(
            household_id=household.id,
            operation="plan_generation",
            model="test-model",
            input_tokens=600,
            output_tokens=500,
        )
    )
    db_session.flush()
    with pytest.raises(HTTPException) as exc:
        check_generation_quota(
            db_session,
            household_id=household.id,
            settings=_cloud_settings(
                cloud_monthly_generation_limit=0, cloud_monthly_token_limit=1000
            ),
        )
    assert exc.value.status_code == 429
    assert "tokens" in exc.value.detail


def test_self_hosted_never_limited(db_session: Session) -> None:
    _user, household, member = make_household(db_session, allergen=None)
    for _ in range(3):
        enqueue_plan(db_session, household, member, budget="500")
    # self_hosted with an impossibly small limit -> still no limit applied.
    check_generation_quota(
        db_session,
        household_id=household.id,
        settings=Settings(deployment_mode="self_hosted", cloud_monthly_generation_limit=1),
    )


def test_disabled_limits_are_noop(db_session: Session) -> None:
    _user, household, member = make_household(db_session, allergen=None)
    for _ in range(3):
        enqueue_plan(db_session, household, member, budget="500")
    # Cloud mode but all limits <= 0 -> disabled -> allowed.
    check_generation_quota(
        db_session,
        household_id=household.id,
        settings=_cloud_settings(
            cloud_monthly_generation_limit=0,
            cloud_daily_generation_limit=0,
            cloud_monthly_token_limit=0,
        ),
    )


# --------------------------------------------------------------------------- #
# Endpoint-level: generate returns 429 over the limit
# --------------------------------------------------------------------------- #
def _email() -> str:
    return f"quota-{uuid.uuid4().hex[:12]}@example.com"


def _client(db_session: Session) -> TestClient:
    from cestaplan_api.routers import auth, households, plans

    app = FastAPI()
    for module in (auth, households, plans, usage_router):
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


def _requirements() -> list[dict[str, Any]]:
    return [{"meal_type": "lunch", "requested_count": 1, "default_servings": 1}]


def test_generate_endpoint_returns_429_when_over_limit(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Cloud, monthly limit = 1: first generate allowed, second exceeds -> 429.
    monkeypatch.setattr(
        quota_service,
        "get_settings",
        lambda: _cloud_settings(cloud_monthly_generation_limit=1),
    )
    client = _client(db_session)
    email = _email()
    register(client, email)
    token = login(client, email)
    hh = client.post("/api/v1/households", json={"name": "Casa"}, headers=csrf(token)).json()

    start = date.today()
    body = {
        "household_id": hh["id"],
        "start_date": start.isoformat(),
        "end_date": (start + timedelta(days=2)).isoformat(),
        "budget_amount": "200",
        "requirements": _requirements(),
    }
    first = client.post("/api/v1/plans/generate", json=body, headers=csrf(token))
    assert first.status_code == 202, first.text

    second = client.post("/api/v1/plans/generate", json=body, headers=csrf(token))
    assert second.status_code == 429, second.text
    assert "límite mensual" in second.json()["detail"]


def test_generate_endpoint_allows_in_self_hosted(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        quota_service,
        "get_settings",
        lambda: Settings(deployment_mode="self_hosted", cloud_monthly_generation_limit=1),
    )
    client = _client(db_session)
    email = _email()
    register(client, email)
    token = login(client, email)
    hh = client.post("/api/v1/households", json={"name": "Casa"}, headers=csrf(token)).json()
    start = date.today()
    body = {
        "household_id": hh["id"],
        "start_date": start.isoformat(),
        "end_date": (start + timedelta(days=2)).isoformat(),
        "budget_amount": "200",
        "requirements": _requirements(),
    }
    # Many generations, never limited in self_hosted.
    for _ in range(3):
        resp = client.post("/api/v1/plans/generate", json=body, headers=csrf(token))
        assert resp.status_code == 202, resp.text


# --------------------------------------------------------------------------- #
# GET /api/v1/usage/me: server-side aggregates, never leaks the key
# --------------------------------------------------------------------------- #
def test_usage_me_returns_server_side_aggregates_without_key_leak(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret_key = "sk-super-secret-managed-key"
    monkeypatch.setattr(
        usage_router,
        "get_settings",
        lambda: _cloud_settings(openai_api_key=secret_key),
    )
    client = _client(db_session)
    email = _email()
    register(client, email)
    token = login(client, email)
    hh = client.post("/api/v1/households", json={"name": "Casa"}, headers=csrf(token)).json()

    # One generation (a run) + a ledger row with real tokens for this household.
    start = date.today()
    client.post(
        "/api/v1/plans/generate",
        json={
            "household_id": hh["id"],
            "start_date": start.isoformat(),
            "end_date": (start + timedelta(days=2)).isoformat(),
            "budget_amount": "200",
            "requirements": _requirements(),
        },
        headers=csrf(token),
    )
    household = db_session.execute(
        select(Household).where(Household.public_id == uuid.UUID(hh["id"]))
    ).scalar_one()
    db_session.add(
        UsageLedger(
            household_id=household.id,
            operation="plan_generation",
            model="test-model",
            input_tokens=100,
            output_tokens=50,
        )
    )
    db_session.flush()

    resp = client.get(f"/api/v1/usage/me?household_id={hh['id']}")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["generations"]["month"] >= 1
    assert data["tokens"] == {"input": 100, "output": 50, "total": 150}
    # No price table -> cost is not fabricated.
    assert data["estimated_cost"]["amount"] is None
    # Cloud mode surfaces the configured limits.
    assert data["limits"]["monthly_generation_limit"] == 100
    # The managed API key never appears anywhere in the response.
    assert secret_key not in resp.text


def test_usage_me_requires_membership(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(usage_router, "get_settings", lambda: _cloud_settings())
    client = _client(db_session)
    owner_email = _email()
    register(client, owner_email)
    owner_token = login(client, owner_email)
    hh = client.post(
        "/api/v1/households", json={"name": "Casa"}, headers=csrf(owner_token)
    ).json()

    other_email = _email()
    register(client, other_email)
    login(client, other_email)
    resp = client.get(f"/api/v1/usage/me?household_id={hh['id']}")
    assert resp.status_code == 404
