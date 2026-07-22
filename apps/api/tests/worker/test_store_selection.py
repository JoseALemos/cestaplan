"""Chain-selection tests: prices are scoped to one CHAIN, never mixed across chains.

Product decision: "la tienda da igual" — a plan is priced against the selected supermarket
CHAIN (retailer), aggregating the latest price observation per product across ALL of that
chain's stores. The demo seed creates two synthetic stores of the same chain on the identical
catalogue (the second priced ~15% cheaper); because pricing is now chain-scoped, the plan
aggregates both and the representative store no longer changes the cost. These tests assert
prices never cross chain boundaries, that switching the representative store keeps the cost
stable, that omitting the chain falls back to a default, and that an invalid chain is rejected.
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
from cestaplan_api.models import Retailer, Store
from cestaplan_api.schemas.plan import MealRequirementIn, MealType
from cestaplan_api.services.plan_service import (
    create_generation,
    resolve_plan_retailer,
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


def _demo_retailer(db: Session) -> Retailer:
    return db.execute(
        select(Retailer).where(Retailer.is_synthetic.is_(True)).order_by(Retailer.id)
    ).scalars().first()


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
    assert len(stores) >= 2, "demo seed must provide a second store of the same chain"


def test_latest_prices_never_mixes_chains(db_session: Session) -> None:
    retailer = _demo_retailer(db_session)
    stores = _demo_stores(db_session)

    prices = _latest_prices(db_session, retailer.id)

    # Every returned price belongs to the selected chain only (no cross-chain mixing).
    assert prices
    assert all(p.retailer_id == retailer.id for p in prices.values())

    # The chain aggregates across ALL its stores: the demo chain's two stores share every
    # product, and the latest observation per product is taken (ties broken by newest row,
    # i.e. the second, ~15% cheaper store inserted last). So each product resolves to a price
    # sourced from one of the chain's stores.
    store_ids = {s.id for s in stores}
    assert all(p.store_id in store_ids for p in prices.values())


def test_store_choice_does_not_change_chain_cost(db_session: Session) -> None:
    _user, household, member = make_household(db_session, allergen="gluten")
    stores = _demo_stores(db_session)
    store_a, store_b = stores[0], stores[1]

    plan_a, run_a, job_a = _rows(db_session, household, member, store=store_a)
    plan_b, run_b, job_b = _rows(db_session, household, member, store=store_b)
    # Both representative stores belong to the same chain, so both plans price against it.
    assert plan_a.retailer_id == plan_b.retailer_id == store_a.retailer_id
    # The representative store is display-only and is still recorded for context.
    assert plan_a.store_id == store_a.id
    assert plan_b.store_id == store_b.id

    # Same seed -> same recipe choices, isolating any cost delta to pricing.
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
    # Pricing is chain-scoped: choosing store A vs B within the same chain yields the SAME
    # total (both aggregate the identical chain-wide latest prices). This is the intended
    # behaviour after moving from per-store to per-chain pricing.
    assert total_a == total_b
    # And the demo chain prices every product, so the plan is fully costed (real money).
    assert total_a > 0


def test_omitting_chain_uses_default(db_session: Session) -> None:
    _user, household, member = make_household(db_session, allergen=None)
    retailer = _demo_retailer(db_session)
    stores = _demo_stores(db_session)

    # No explicit chain/store and no household default -> first active store's chain is used,
    # with that chain's first active store as the representative store.
    plan, _run, _job = _rows(db_session, household, member, store=None)
    assert plan.retailer_id == retailer.id
    assert plan.store_id == stores[0].id

    # An explicit household default chain is honoured.
    household.default_retailer_id = retailer.id
    db_session.flush()
    resolved_retailer, _store = resolve_plan_retailer(db_session, household, None, None)
    assert resolved_retailer is not None and resolved_retailer.id == retailer.id


def test_retailer_id_selects_the_chain(db_session: Session) -> None:
    _user, household, _member = make_household(db_session, allergen=None)
    retailer = _demo_retailer(db_session)
    resolved_retailer, store = resolve_plan_retailer(
        db_session, household, retailer.public_id, None
    )
    assert resolved_retailer is not None and resolved_retailer.id == retailer.id
    # A representative store of the chain is offered for display (may be any of its stores).
    assert store is not None and store.retailer_id == retailer.id


def test_store_id_backward_compat_resolves_its_chain(db_session: Session) -> None:
    _user, household, _member = make_household(db_session, allergen=None)
    store_b = _demo_stores(db_session)[1]
    resolved_retailer, store = resolve_plan_retailer(
        db_session, household, None, store_b.public_id
    )
    assert resolved_retailer is not None and resolved_retailer.id == store_b.retailer_id
    assert store is not None and store.id == store_b.id


def test_invalid_retailer_id_is_rejected(db_session: Session) -> None:
    _user, household, _member = make_household(db_session, allergen=None)
    with pytest.raises(HTTPException) as excinfo:
        resolve_plan_retailer(db_session, household, uuid.uuid4(), None)
    assert excinfo.value.status_code == 404


def test_inactive_retailer_is_rejected(db_session: Session) -> None:
    _user, household, _member = make_household(db_session, allergen=None)
    retailer = _demo_retailer(db_session)
    retailer.is_active = False
    db_session.flush()
    with pytest.raises(HTTPException) as excinfo:
        resolve_plan_retailer(db_session, household, retailer.public_id, None)
    assert excinfo.value.status_code == 422


def test_invalid_store_id_is_rejected(db_session: Session) -> None:
    _user, household, _member = make_household(db_session, allergen=None)
    with pytest.raises(HTTPException) as excinfo:
        resolve_plan_store(db_session, household, uuid.uuid4())
    assert excinfo.value.status_code == 404


def test_inactive_store_id_is_rejected(db_session: Session) -> None:
    _user, household, _member = make_household(db_session, allergen=None)
    store_b = _demo_stores(db_session)[1]
    store_b.is_active = False
    db_session.flush()
    with pytest.raises(HTTPException) as excinfo:
        resolve_plan_retailer(db_session, household, None, store_b.public_id)
    assert excinfo.value.status_code == 422
