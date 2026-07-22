"""planning_context: members' DietaryProfile goals aggregate into the plan target."""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from cestaplan_api.models import DietaryProfile, HouseholdMember
from cestaplan_api.services.planning_context import build_plan_input

from .factory import enqueue_plan, make_household


def _owner_profile(db: Session, household_id: int) -> DietaryProfile:
    profile = db.execute(
        select(DietaryProfile).where(DietaryProfile.household_id == household_id)
    ).scalars().first()
    assert profile is not None
    return profile


def test_no_member_goals_yields_no_target(db_session: Session) -> None:
    _user, household, member = make_household(db_session, allergen=None)
    meal_plan, run, _job = enqueue_plan(db_session, household, member)

    plan_input = build_plan_input(db_session, meal_plan, seed=run.seed)
    assert plan_input.nutrition_target is None


def test_member_goals_aggregate_scaled_by_relative_serving(db_session: Session) -> None:
    _user, household, member = make_household(db_session, allergen=None)

    # Owner: a per-person goal at a 1.5x portion size -> scaled by relative_serving.
    profile = _owner_profile(db_session, household.id)
    profile.protein_target_g = Decimal("100")
    profile.energy_target_kcal = Decimal("2000")
    owner = db_session.get(HouseholdMember, member.id)
    assert owner is not None
    owner.relative_serving = Decimal("1.5")
    db_session.flush()

    meal_plan, run, _job = enqueue_plan(db_session, household, member)
    plan_input = build_plan_input(db_session, meal_plan, seed=run.seed)

    target = plan_input.nutrition_target
    assert target is not None
    # 100 g * 1.5 serving = 150 g/day; 2000 kcal * 1.5 = 3000 kcal/day.
    assert target.protein_g == Decimal("150.0")
    assert target.kcal == Decimal("3000.0")
    # Macros no member set stay None.
    assert target.carbs_g is None
    assert target.fat_g is None
