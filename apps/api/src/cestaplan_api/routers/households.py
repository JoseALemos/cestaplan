"""Household router: household CRUD and member/dietary-profile management.

Authorization is by household membership and role, verified on the server for every
route (no IDOR): households are addressed by public UUID and resolved through
:func:`cestaplan_api.deps.get_household_context`. Roles per docs/SECURITY.md §3.1 —
owner manages members and the household; owner/editor edit dietary data; viewer is
read-only.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import delete, func, select

from cestaplan_api.deps import (
    CurrentUser,
    DbSession,
    HouseholdCtx,
    HouseholdCtxEditor,
    HouseholdCtxOwner,
    verify_csrf,
)
from cestaplan_api.models import DietaryProfile, Equipment, Household, HouseholdMember
from cestaplan_api.schemas.household import (
    EquipmentResponse,
    EquipmentSet,
    HouseholdCreate,
    HouseholdResponse,
    MemberCreate,
    MemberResponse,
    MemberUpdate,
)
from cestaplan_api.services.audit import record_audit
from cestaplan_api.services.household import (
    apply_nutrition_goal,
    build_allergies,
    build_preferences,
)

router = APIRouter(prefix="/api/v1/households", tags=["households"])


def _member_count(db: DbSession, household_id: int) -> int:
    return db.execute(
        select(func.count(HouseholdMember.id)).where(
            HouseholdMember.household_id == household_id
        )
    ).scalar_one()


# --------------------------------------------------------------------------- #
# Household CRUD
# --------------------------------------------------------------------------- #
@router.post(
    "",
    response_model=HouseholdResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(verify_csrf)],
)
def create_household(
    payload: HouseholdCreate, user: CurrentUser, db: DbSession
) -> HouseholdResponse:
    """Create a household; the caller becomes its owner and first member."""
    now = datetime.now(UTC)
    household = Household(name=payload.name, owner_user_id=user.id, currency=payload.currency)
    db.add(household)
    db.flush()

    member = HouseholdMember(
        household_id=household.id,
        user_id=user.id,
        role="owner",
        display_name=user.display_name,
        is_eater=True,
        joined_at=now,
    )
    db.add(member)
    record_audit(db, action="household.create", actor_user_id=user.id,
                 household_id=household.id, entity_type="household",
                 entity_public_id=household.public_id)
    return HouseholdResponse.from_model(household, my_role="owner", member_count=1)


@router.get("", response_model=list[HouseholdResponse])
def list_my_households(user: CurrentUser, db: DbSession) -> list[HouseholdResponse]:
    """List households the caller belongs to, with the caller's role in each."""
    rows = db.execute(
        select(Household, HouseholdMember.role)
        .join(HouseholdMember, HouseholdMember.household_id == Household.id)
        .where(HouseholdMember.user_id == user.id, Household.deleted_at.is_(None))
        .order_by(Household.created_at)
    ).all()
    return [
        HouseholdResponse.from_model(
            household, my_role=role, member_count=_member_count(db, household.id)
        )
        for household, role in rows
    ]


@router.get("/{household_id}", response_model=HouseholdResponse)
def get_household(ctx: HouseholdCtx, db: DbSession) -> HouseholdResponse:
    """Get a single household the caller belongs to."""
    return HouseholdResponse.from_model(
        ctx.household,
        my_role=ctx.role,
        member_count=_member_count(db, ctx.household.id),
    )


@router.patch(
    "/{household_id}", response_model=HouseholdResponse, dependencies=[Depends(verify_csrf)]
)
def update_household(
    payload: HouseholdCreate,
    ctx: HouseholdCtxOwner,
    user: CurrentUser,
    db: DbSession,
) -> HouseholdResponse:
    """Rename a household or change its currency (owner only)."""
    ctx.household.name = payload.name
    ctx.household.currency = payload.currency
    record_audit(db, action="household.update", actor_user_id=user.id,
                 household_id=ctx.household.id, entity_type="household",
                 entity_public_id=ctx.household.public_id)
    return HouseholdResponse.from_model(
        ctx.household, my_role=ctx.role, member_count=_member_count(db, ctx.household.id)
    )


# --------------------------------------------------------------------------- #
# Members / dietary profiles
# --------------------------------------------------------------------------- #
@router.post(
    "/{household_id}/members",
    response_model=MemberResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(verify_csrf)],
)
def add_member(
    payload: MemberCreate,
    ctx: HouseholdCtxOwner,
    user: CurrentUser,
    db: DbSession,
) -> MemberResponse:
    """Add an eater to the household with their dietary profile (owner only)."""
    now = datetime.now(UTC)
    member = HouseholdMember(
        household_id=ctx.household.id,
        user_id=None,
        role=payload.role,
        display_name=payload.display_name,
        is_eater=payload.is_eater,
        joined_at=now,
    )
    db.add(member)
    db.flush()

    profile = DietaryProfile(
        household_id=ctx.household.id,
        household_member_id=member.id,
        diet_type=payload.diet_type,
        notes=payload.notes,
    )
    apply_nutrition_goal(profile, payload.nutrition_goal)
    profile.allergies = build_allergies(payload.allergies, payload.intolerances)
    profile.food_preferences = build_preferences(
        payload.preferences, payload.rejected_ingredients
    )
    db.add(profile)
    db.flush()

    record_audit(db, action="household.member.add", actor_user_id=user.id,
                 household_id=ctx.household.id, entity_type="household_member",
                 entity_public_id=member.public_id)
    return MemberResponse.from_model(member, profile)


@router.get("/{household_id}/equipment", response_model=list[EquipmentResponse])
def list_equipment(ctx: HouseholdCtx, db: DbSession) -> list[EquipmentResponse]:
    """List the kitchen equipment declared for the household."""
    rows = db.execute(
        select(Equipment)
        .where(Equipment.household_id == ctx.household.id)
        .order_by(Equipment.equipment_code)
    ).scalars().all()
    return [EquipmentResponse.from_model(e) for e in rows]


@router.put(
    "/{household_id}/equipment",
    response_model=list[EquipmentResponse],
    dependencies=[Depends(verify_csrf)],
)
def set_equipment(
    payload: EquipmentSet,
    ctx: HouseholdCtxEditor,
    user: CurrentUser,
    db: DbSession,
) -> list[EquipmentResponse]:
    """Replace the household's declared equipment (editor+).

    The deterministic engine filters recipe candidates by available equipment, so an
    empty declaration means "no appliances" and will make most plans infeasible.
    """
    # Full replacement: drop existing rows, insert the new set (dedup by code).
    db.execute(delete(Equipment).where(Equipment.household_id == ctx.household.id))
    seen: set[str] = set()
    created: list[Equipment] = []
    for item in payload.equipment:
        if item.equipment_code in seen:
            continue
        seen.add(item.equipment_code)
        row = Equipment(
            household_id=ctx.household.id,
            equipment_code=item.equipment_code,
            available=item.available,
        )
        db.add(row)
        created.append(row)
    db.flush()
    record_audit(db, action="household.equipment.set", actor_user_id=user.id,
                 household_id=ctx.household.id, entity_type="equipment",
                 entity_public_id=ctx.household.public_id)
    return [EquipmentResponse.from_model(e) for e in created]


@router.get("/{household_id}/members", response_model=list[MemberResponse])
def list_members(ctx: HouseholdCtx, db: DbSession) -> list[MemberResponse]:
    """List all members of the household with their dietary profiles."""
    members = db.execute(
        select(HouseholdMember)
        .where(HouseholdMember.household_id == ctx.household.id)
        .order_by(HouseholdMember.joined_at)
    ).scalars().all()
    return [
        MemberResponse.from_model(
            m, m.dietary_profiles[0] if m.dietary_profiles else None
        )
        for m in members
    ]


@router.patch(
    "/{household_id}/members/{member_id}",
    response_model=MemberResponse,
    dependencies=[Depends(verify_csrf)],
)
def update_member(
    member_id: uuid.UUID,
    payload: MemberUpdate,
    ctx: HouseholdCtxEditor,
    user: CurrentUser,
    db: DbSession,
) -> MemberResponse:
    """Update a member's servings/preferences (editor+). Role changes are owner-only."""
    member = db.execute(
        select(HouseholdMember).where(
            HouseholdMember.public_id == member_id,
            HouseholdMember.household_id == ctx.household.id,
        )
    ).scalar_one_or_none()
    if member is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Miembro no encontrado")

    # Role management is reserved to the owner (docs/SECURITY.md §3.1).
    if payload.role is not None and payload.role != member.role and ctx.role != "owner":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Solo el propietario puede cambiar roles",
        )

    if payload.display_name is not None:
        member.display_name = payload.display_name
    if payload.is_eater is not None:
        member.is_eater = payload.is_eater
    if payload.role is not None:
        member.role = payload.role

    profile = member.dietary_profiles[0] if member.dietary_profiles else None
    if profile is None:
        profile = DietaryProfile(
            household_id=ctx.household.id, household_member_id=member.id
        )
        db.add(profile)

    if payload.diet_type is not None:
        profile.diet_type = payload.diet_type
    if payload.notes is not None:
        profile.notes = payload.notes
    apply_nutrition_goal(profile, payload.nutrition_goal)

    # Collections replace wholesale when provided (cascade delete-orphan removes old rows).
    if payload.allergies is not None or payload.intolerances is not None:
        profile.allergies = build_allergies(
            payload.allergies or [], payload.intolerances or []
        )
    if payload.preferences is not None or payload.rejected_ingredients is not None:
        profile.food_preferences = build_preferences(
            payload.preferences or [], payload.rejected_ingredients or []
        )

    db.flush()
    record_audit(db, action="household.member.update", actor_user_id=user.id,
                 household_id=ctx.household.id, entity_type="household_member",
                 entity_public_id=member.public_id)
    return MemberResponse.from_model(member, profile)
