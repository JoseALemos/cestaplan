"""Household and member (dietary profile) request/response schemas.

Dietary data (allergies, intolerances, preferences, nutrition goals) is sensitive
application data (docs/PRIVACY.md §1); it is only collected for the planning function
the user requested. Nutrition figures are ``Decimal`` and serialised as strings, never
floats, consistent with the project's no-float rule.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field

Role = Literal["owner", "editor", "viewer"]
AllergySeverity = Literal["intolerance", "allergy", "anaphylaxis"]
PreferenceSubject = Literal["ingredient", "cuisine", "tag"]
PreferenceSentiment = Literal["like", "dislike", "avoid"]

_MAX_COLLECTION = 50


def _decimal_str(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None


# --------------------------------------------------------------------------- #
# Household
# --------------------------------------------------------------------------- #
class HouseholdCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    currency: str = Field(default="EUR", min_length=3, max_length=3)


class HouseholdResponse(BaseModel):
    id: uuid.UUID
    name: str
    currency: str
    my_role: Role
    member_count: int
    created_at: datetime

    @classmethod
    def from_model(cls, household, my_role: str, member_count: int) -> HouseholdResponse:
        return cls(
            id=household.public_id,
            name=household.name,
            currency=household.currency,
            my_role=my_role,  # type: ignore[arg-type]
            member_count=member_count,
            created_at=household.created_at,
        )


# --------------------------------------------------------------------------- #
# Dietary profile sub-objects
# --------------------------------------------------------------------------- #
class AllergyIn(BaseModel):
    allergen_code: str = Field(min_length=1, max_length=100)
    severity: AllergySeverity = "allergy"
    avoid_traces: bool = True
    notes: str | None = Field(default=None, max_length=500)


class PreferenceIn(BaseModel):
    subject_type: PreferenceSubject = "ingredient"
    subject_ref: str = Field(min_length=1, max_length=200)
    sentiment: PreferenceSentiment = "like"
    weight: Decimal | None = Field(default=None, ge=0, le=10)


class NutritionGoalIn(BaseModel):
    energy_target_kcal: Decimal | None = Field(default=None, ge=0, le=20000)
    protein_target_g: Decimal | None = Field(default=None, ge=0, le=2000)
    carb_target_g: Decimal | None = Field(default=None, ge=0, le=2000)
    fat_target_g: Decimal | None = Field(default=None, ge=0, le=2000)


class AllergyResponse(BaseModel):
    allergen_code: str
    severity: str
    avoid_traces: bool
    notes: str | None

    @classmethod
    def from_model(cls, allergy) -> AllergyResponse:
        return cls(
            allergen_code=allergy.allergen_code,
            severity=allergy.severity,
            avoid_traces=allergy.avoid_traces,
            notes=allergy.notes,
        )


class PreferenceResponse(BaseModel):
    subject_type: str
    subject_ref: str
    sentiment: str
    weight: str | None

    @classmethod
    def from_model(cls, pref) -> PreferenceResponse:
        return cls(
            subject_type=pref.subject_type,
            subject_ref=pref.subject_ref,
            sentiment=pref.sentiment,
            weight=_decimal_str(pref.weight),
        )


class DietaryProfileResponse(BaseModel):
    diet_type: str | None
    energy_target_kcal: str | None
    protein_target_g: str | None
    carb_target_g: str | None
    fat_target_g: str | None
    notes: str | None
    allergies: list[AllergyResponse]
    preferences: list[PreferenceResponse]

    @classmethod
    def from_model(cls, profile) -> DietaryProfileResponse:
        return cls(
            diet_type=profile.diet_type,
            energy_target_kcal=_decimal_str(profile.energy_target_kcal),
            protein_target_g=_decimal_str(profile.protein_target_g),
            carb_target_g=_decimal_str(profile.carb_target_g),
            fat_target_g=_decimal_str(profile.fat_target_g),
            notes=profile.notes,
            allergies=[AllergyResponse.from_model(a) for a in profile.allergies],
            preferences=[PreferenceResponse.from_model(p) for p in profile.food_preferences],
        )


# --------------------------------------------------------------------------- #
# Members
# --------------------------------------------------------------------------- #
class MemberCreate(BaseModel):
    """Add an eater to the household with their dietary profile.

    ``intolerances`` is a shorthand: each code becomes an allergy row with severity
    ``intolerance``. ``rejected_ingredients`` is a shorthand for ``avoid`` preferences.
    ``allergies`` / ``preferences`` give full control when needed.
    """

    display_name: str = Field(min_length=1, max_length=200)
    role: Role = "viewer"
    is_eater: bool = True
    diet_type: str | None = Field(default=None, max_length=100)
    notes: str | None = Field(default=None, max_length=1000)
    nutrition_goal: NutritionGoalIn | None = None
    allergies: list[AllergyIn] = Field(default_factory=list, max_length=_MAX_COLLECTION)
    intolerances: list[str] = Field(default_factory=list, max_length=_MAX_COLLECTION)
    preferences: list[PreferenceIn] = Field(default_factory=list, max_length=_MAX_COLLECTION)
    rejected_ingredients: list[str] = Field(
        default_factory=list, max_length=_MAX_COLLECTION
    )


class MemberUpdate(BaseModel):
    """Partial update. Only provided fields change; provided collections replace the
    existing set wholesale (send ``[]`` to clear)."""

    display_name: str | None = Field(default=None, min_length=1, max_length=200)
    role: Role | None = None
    is_eater: bool | None = None
    diet_type: str | None = Field(default=None, max_length=100)
    notes: str | None = Field(default=None, max_length=1000)
    nutrition_goal: NutritionGoalIn | None = None
    allergies: list[AllergyIn] | None = Field(default=None, max_length=_MAX_COLLECTION)
    intolerances: list[str] | None = Field(default=None, max_length=_MAX_COLLECTION)
    preferences: list[PreferenceIn] | None = Field(default=None, max_length=_MAX_COLLECTION)
    rejected_ingredients: list[str] | None = Field(default=None, max_length=_MAX_COLLECTION)


class MemberResponse(BaseModel):
    id: uuid.UUID
    display_name: str | None
    role: str
    is_eater: bool
    profile: DietaryProfileResponse | None

    @classmethod
    def from_model(cls, member, profile) -> MemberResponse:
        return cls(
            id=member.public_id,
            display_name=member.display_name,
            role=member.role,
            is_eater=member.is_eater,
            profile=DietaryProfileResponse.from_model(profile) if profile else None,
        )


# --------------------------------------------------------------------------- #
# Equipment (kitchen appliances available in the household)
# --------------------------------------------------------------------------- #
# Known codes align with docs (§6) and the demo recipes' required_equipment.
KnownEquipment = Literal[
    "oven",
    "microwave",
    "airfryer",
    "stovetop",
    "toaster",
    "pot",
    "pressure_cooker",
    "blender",
    "food_processor",
    "griddle",
    "barbecue",
]


class EquipmentIn(BaseModel):
    equipment_code: KnownEquipment
    available: bool = True


class EquipmentSet(BaseModel):
    """Full replacement of the household's declared equipment."""

    equipment: list[EquipmentIn] = Field(default_factory=list, max_length=_MAX_COLLECTION)


class EquipmentResponse(BaseModel):
    equipment_code: str
    available: bool

    @classmethod
    def from_model(cls, equipment) -> EquipmentResponse:
        return cls(equipment_code=equipment.equipment_code, available=equipment.available)
