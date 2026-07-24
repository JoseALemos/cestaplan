"""Preflight against a real DB + worker integration: a plan is priced against a SINGLE chain,
retailer_id=None is stopped before the catalogue is built, and the solver never runs on an
impossible precondition."""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from cestaplan_api.models import PlannedMeal, ProductPrice, Retailer
from cestaplan_api.services.planner_preflight import PreflightCode, run_preflight
from cestaplan_worker import processor

from .factory import enqueue_plan, make_household


def _demo_retailer(db: Session) -> Retailer:
    r = db.execute(
        select(Retailer).where(Retailer.is_synthetic.is_(True)).order_by(Retailer.id)
    ).scalars().first()
    assert r is not None, "demo seed must provide a synthetic retailer with prices"
    return r


def _planned(db: Session, meal_plan_id: int) -> list[PlannedMeal]:
    return list(
        db.execute(
            select(PlannedMeal).where(PlannedMeal.meal_plan_id == meal_plan_id)
        ).scalars().all()
    )


# --------------------------------------------------------------------------- #
# §4 — DB-level preflight scoping
# --------------------------------------------------------------------------- #


def test_retailer_none_is_no_retailer_selected_and_never_mixes_chains(db_session: Session) -> None:
    # Sanity: the demo chain really does have prices that COULD be mixed in globally.
    assert (db_session.scalar(select(func.count()).select_from(ProductPrice)) or 0) > 0

    _user, household, member = make_household(db_session, allergen=None)
    meal_plan, _run, _job = enqueue_plan(db_session, household, member, budget="500")
    meal_plan.retailer_id = None
    db_session.flush()

    out = run_preflight(db_session, meal_plan)
    assert out.code is PreflightCode.NO_RETAILER_SELECTED
    # Global prices were NOT consulted: the diagnostic counts stay 0.
    assert out.candidate_counts["productive_prices"] == 0
    assert out.candidate_counts["costable_recipes"] == 0
    # Never a budget-shaped diagnosis.
    report = out.to_report()
    assert report["minimum_budget"] is None
    actions = report["suggested_actions"]
    assert isinstance(actions, list) and "increase_budget" not in actions


def test_selected_retailer_counts_only_its_own_prices(db_session: Session) -> None:
    retailer = _demo_retailer(db_session)
    _user, household, member = make_household(db_session, allergen=None)
    meal_plan, _run, _job = enqueue_plan(db_session, household, member, budget="500")
    meal_plan.retailer_id = retailer.id
    db_session.flush()

    out = run_preflight(db_session, meal_plan)
    # Scoped to the chain: its prices are counted, so it is NOT no_retailer_selected.
    assert out.code is not PreflightCode.NO_RETAILER_SELECTED
    assert out.candidate_counts["productive_prices"] > 0
    # Every counted price belongs to this chain only.
    scoped = db_session.scalar(
        select(func.count())
        .select_from(ProductPrice)
        .where(ProductPrice.retailer_id == retailer.id)
    )
    assert out.candidate_counts["productive_prices"] == scoped


def test_other_chain_prices_do_not_make_an_empty_chain_pass(db_session: Session) -> None:
    # A fresh, empty chain — the demo chain's prices must NOT leak into it.
    tag = uuid.uuid4().hex[:10]
    empty = Retailer(
        slug=f"empty-{tag}", name="Cadena vacía", adapter_key=f"empty-{tag}", is_synthetic=False
    )
    db_session.add(empty)
    db_session.flush()

    _user, household, member = make_household(db_session, allergen=None)
    meal_plan, _run, _job = enqueue_plan(db_session, household, member, budget="500")
    meal_plan.retailer_id = empty.id
    db_session.flush()

    out = run_preflight(db_session, meal_plan)
    # The empty chain has no catalogue of its own; the demo chain's prices are irrelevant.
    assert out.code in (PreflightCode.RETAILER_WITHOUT_CATALOG, PreflightCode.NO_PRODUCT_PRICES)
    assert out.candidate_counts["productive_prices"] == 0


# --------------------------------------------------------------------------- #
# §5 — worker integration: the solver never runs on an impossible precondition
# --------------------------------------------------------------------------- #


def test_worker_preflight_short_circuits_without_building_or_solving(
    db_session: Session, monkeypatch
) -> None:
    calls = {"build": 0, "solve": 0}

    def _no_build(*_a, **_k):
        calls["build"] += 1
        raise AssertionError("build_plan_input must not run when the preflight fails")

    def _no_solve(*_a, **_k):
        calls["solve"] += 1
        raise AssertionError("generate_plan must not run when the preflight fails")

    monkeypatch.setattr(processor, "build_plan_input", _no_build)
    monkeypatch.setattr(processor, "generate_plan", _no_solve)

    _user, household, member = make_household(db_session, allergen=None)
    meal_plan, run, job = enqueue_plan(db_session, household, member, budget="500")
    meal_plan.retailer_id = None  # impossible precondition: no chain
    db_session.flush()

    processor._execute(db_session, job, run, meal_plan)

    # Neither the context build nor the solver ran.
    assert calls == {"build": 0, "solve": 0}
    assert job.status == "failed"
    assert run.status == "failed"
    assert meal_plan.status == "failed"
    assert run.finished_at is not None
    assert run.result_summary is None
    report = run.infeasibility_report
    assert report is not None
    assert report["code"] == "no_retailer_selected"
    assert report["minimum_budget"] is None
    # last_error is a sanitized message — no traceback, no secret material.
    last_error = job.last_error or ""
    assert "Traceback" not in last_error
    assert "password" not in last_error.lower()
    assert "token" not in last_error.lower()
    # No partial/empty plan was persisted.
    assert _planned(db_session, meal_plan.id) == []

    # A second execution still creates neither a plan nor partial results.
    processor._execute(db_session, job, run, meal_plan)
    assert calls == {"build": 0, "solve": 0}
    assert run.result_summary is None
    assert _planned(db_session, meal_plan.id) == []
