"""Household member / dietary-profile assembly helpers.

Centralises the mapping from the API payload to ORM rows so that member creation and
member update stay consistent. Intolerances are stored as allergies with severity
``intolerance``; rejected ingredients as ``avoid`` food preferences.
"""

from __future__ import annotations

from cestaplan_api.models import Allergy, DietaryProfile, FoodPreference
from cestaplan_api.schemas.household import AllergyIn, NutritionGoalIn, PreferenceIn


def apply_nutrition_goal(profile: DietaryProfile, goal: NutritionGoalIn | None) -> None:
    """Set (or clear) the profile's nutrition targets from the payload."""
    if goal is None:
        return
    profile.energy_target_kcal = goal.energy_target_kcal
    profile.protein_target_g = goal.protein_target_g
    profile.carb_target_g = goal.carb_target_g
    profile.fat_target_g = goal.fat_target_g


def build_allergies(
    allergies: list[AllergyIn], intolerances: list[str]
) -> list[Allergy]:
    """Build allergy rows from full allergy objects plus intolerance shorthands."""
    rows = [
        Allergy(
            allergen_code=a.allergen_code,
            severity=a.severity,
            avoid_traces=a.avoid_traces,
            notes=a.notes,
        )
        for a in allergies
    ]
    rows.extend(
        Allergy(allergen_code=code, severity="intolerance", avoid_traces=False)
        for code in intolerances
    )
    return rows


def build_preferences(
    preferences: list[PreferenceIn], rejected_ingredients: list[str]
) -> list[FoodPreference]:
    """Build preference rows from full objects plus rejected-ingredient shorthands."""
    rows = [
        FoodPreference(
            subject_type=p.subject_type,
            subject_ref=p.subject_ref,
            sentiment=p.sentiment,
            weight=p.weight,
        )
        for p in preferences
    ]
    rows.extend(
        FoodPreference(subject_type="ingredient", subject_ref=name, sentiment="avoid")
        for name in rejected_ingredients
    )
    return rows
