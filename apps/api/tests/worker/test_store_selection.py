"""Store-selection tests: prices are scoped to one store, switching changes cost.

The demo seed creates two synthetic stores of the same chain on the identical catalogue,
the second priced ~15% cheaper. These tests assert the plan is costed against exactly the
chosen store (never mixing prices), that omitting the store falls back to a default, and
that an invalid store is rejected.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from cestaplan_api.deps import HouseholdContext
from cestaplan_api.models import Store
from cestaplan_api.schemas.plan import MealRequirementIn, MealType
from cestaplan_api.services.plan_service import (
    create_generation,
    resolve_plan_store,
)
from cestaplan_api.services.planning_context import _latest_prices
from cestaplan_worker.processor import process_job

from .factory import make_household

_REQUIREMENTS: tuple[tuple[MealType, int], ...] = (
    ("breakfast", 2),
    ("lunch", 4),
    ("snack", 1),
    ("dinner", 3),
)


def _demo_stores(db: Session) -> list[Store]:
    return list(
        db.execute(
            select(Store).where(Store.is_synthetic.is_(True)).order_by(Store.id)
        ).scalars().all()
    )


def _rows(db: Session, household, member, *, store: Store | None):
    ctx = HouseholdContext(household=household, member=member)
    start = date.today()
    reqs = [
        MealRequirementIn(meal_type=mt, requested_count=n, default_servings=2).to_row()
        for mt, n in _REQUIREMENTS
    ]
    return create_generation(
        db,
        ctx,
        start_date=start,
        end_date=start + timedelta(days=6),
        budget_amount=Decimal("500"),
        currency="EUR",
        requirements=reqs,
        store=store,
    )


def test_two_stores_seeded_with_different_prices(db_session: Session) -> None:
    stores = _demo_stores(db_session)
    assert len(stores) >= 2, "demo seed must provide a second store to switch to"


def test_latest_prices_never_mixes_stores(db_session: Session) -> None:
    stores = _demo_stores(db_session)
    store_a, store_b = stores[0], stores[1]

    prices_a = _latest_prices(db_session, store_a.id)
    prices_b = _latest_prices(db_session, store_b.id)

    # Every returned price belongs to the requested store only.
    assert prices_a and prices_b
    assert all(p.store_id == store_a.id for p in prices_a.values())
    assert all(p.store_id == store_b.id for p in prices_b.values())

    # The same product resolves to a different (cheaper) price in the second store.
    common = set(prices_a) & set(prices_b)
    assert common
    sample = next(iter(common))
    assert prices_b[sample].amount < prices_a[sample].amount


def test_switching_store_changes_plan_cost(db_session: Session) -> None:
    _user, household, member = make_household(db_session, allergen="gluten")
    stores = _demo_stores(db_session)
    store_a, store_b = stores[0], stores[1]

    plan_a, run_a, job_a = _rows(db_session, household, member, store=store_a)
    plan_b, run_b, job_b = _rows(db_session, household, member, store=store_b)
    assert plan_a.store_id == store_a.id
    assert plan_b.store_id == store_b.id

    # Same seed -> same recipe choices, so any cost delta is purely the store's prices.
    run_b.seed = run_a.seed
    db_session.flush()

    process_job(job_a, db_session)
    process_job(job_b, db_session)
    assert run_a.status == "completed"
    assert run_b.status == "completed"
    assert run_a.result_summary is not None
    assert run_b.result_summary is not None

    total_a = Decimal(run_a.result_summary["cost_total"]["total"])
    total_b = Decimal(run_b.result_summary["cost_total"]["total"])
    # The second store is ~15% cheaper on the identical catalogue.
    assert total_b < total_a


def test_omitting_store_uses_default_store(db_session: Session) -> None:
    _user, household, member = make_household(db_session, allergen=None)
    stores = _demo_stores(db_session)

    # No explicit store and no household default -> first active store is used.
    plan, _run, _job = _rows(db_session, household, member, store=None)
    assert plan.store_id == stores[0].id

    # An explicit household default is honoured over the first store.
    household.default_store_id = stores[1].id
    db_session.flush()
    resolved = resolve_plan_store(db_session, household, None)
    assert resolved is not None and resolved.id == stores[1].id


def test_invalid_store_id_is_rejected(db_session: Session) -> None:
    _user, household, _member = make_household(db_session, allergen=None)
    with pytest.raises(HTTPException) as excinfo:
        resolve_plan_store(db_session, household, uuid.uuid4())
    assert excinfo.value.status_code == 404


def test_inactive_store_is_rejected(db_session: Session) -> None:
    _user, household, _member = make_household(db_session, allergen=None)
    store_b = _demo_stores(db_session)[1]
    store_b.is_active = False
    db_session.flush()
    with pytest.raises(HTTPException) as excinfo:
        resolve_plan_store(db_session, household, store_b.public_id)
    assert excinfo.value.status_code == 422
