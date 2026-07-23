"""Test factories: build a household + an enqueued plan the worker can process."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from cestaplan_api.deps import HouseholdContext
from cestaplan_api.models import (
    Allergy,
    DietaryProfile,
    Equipment,
    GenerationJob,
    Household,
    HouseholdMember,
    MealPlan,
    OptimizationRun,
    User,
)
from cestaplan_api.schemas.plan import MealRequirementIn, MealType
from cestaplan_api.services.plan_service import create_generation

_EQUIPMENT = ("toaster", "stovetop", "blender", "oven")
_DEFAULT_REQUIREMENTS: tuple[tuple[MealType, int], ...] = (
    ("breakfast", 2),
    ("lunch", 4),
    ("snack", 1),
    ("dinner", 3),
)


def make_household(
    db: Session, *, allergen: str | None = "gluten"
) -> tuple[User, Household, HouseholdMember]:
    """A 2-eater household with kitchen equipment and (optionally) one hard allergy."""
    now = datetime.now(UTC)
    user = User(
        email=f"worker-{uuid.uuid4().hex[:12]}@example.com",
        password_hash="x",
        display_name="Owner",
    )
    db.add(user)
    db.flush()

    household = Household(name="Casa", owner_user_id=user.id, currency="EUR")
    db.add(household)
    db.flush()

    owner_member = HouseholdMember(
        household_id=household.id,
        user_id=user.id,
        role="owner",
        display_name="Alex",
        is_eater=True,
        joined_at=now,
        relative_serving=Decimal("1.0"),
    )
    other_member = HouseholdMember(
        household_id=household.id,
        role="viewer",
        display_name="Sam",
        is_eater=True,
        joined_at=now,
        relative_serving=Decimal("1.0"),
    )
    db.add_all([owner_member, other_member])
    db.flush()

    profile = DietaryProfile(household_id=household.id, household_member_id=owner_member.id)
    db.add(profile)
    db.flush()
    if allergen is not None:
        profile.allergies = [
            Allergy(allergen_code=allergen, severity="allergy", avoid_traces=True)
        ]

    for code in _EQUIPMENT:
        db.add(Equipment(household_id=household.id, equipment_code=code, available=True))
    db.flush()
    return user, household, owner_member


def enqueue_plan(
    db: Session,
    household: Household,
    member: HouseholdMember,
    *,
    budget: str = "500",
    budget_priority: str = "waste",
    requirements: tuple[tuple[MealType, int], ...] = _DEFAULT_REQUIREMENTS,
) -> tuple[MealPlan, OptimizationRun, GenerationJob]:
    """Create a plan + requirements + queued run/job (2 bf / 4 lunch / 1 snack / 3 dinner)."""
    ctx = HouseholdContext(household=household, member=member)
    start = date.today()
    end = start + timedelta(days=6)
    rows = [
        MealRequirementIn(meal_type=mt, requested_count=count, default_servings=2).to_row()
        for mt, count in requirements
    ]
    return create_generation(
        db,
        ctx,
        start_date=start,
        end_date=end,
        budget_amount=Decimal(budget),
        currency="EUR",
        requirements=rows,
        budget_priority=budget_priority,
    )
