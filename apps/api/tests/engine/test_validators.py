"""Allergen and dietary validators (OPTIMIZATION.md §2.3, §2.4)."""

from __future__ import annotations

from cestaplan_engine.validators import AllergenValidator, DietaryRestrictionValidator

from .builders import ingredient, member, product, recipe


def test_allergen_hard_rejection_declared():
    r = recipe("r1", {"lunch"}, [ingredient("pasta", "200", "g")], allergens={"gluten"})
    m = member("A", allergens={"gluten"})
    result = AllergenValidator().validate(r, [m])
    assert result.valid is False
    assert any("gluten" in v for v in result.hard_violations)


def test_allergen_derived_from_catalog():
    prod = product("milk", "milk", [], allergens={"lactose"})
    r = recipe("r1", {"breakfast"}, [ingredient("milk", "200", "ml")])
    m = member("A", allergens={"lactose"})
    result = AllergenValidator([prod]).validate(r, [m])
    assert result.valid is False


def test_allergen_safe_recipe_passes():
    r = recipe("r1", {"lunch"}, [ingredient("rice", "200", "g")])
    m = member("A", allergens={"gluten"})
    result = AllergenValidator().validate(r, [m])
    assert result.valid is True


def test_allergen_missing_data_warns_conservatively():
    r = recipe("r1", {"lunch"}, [ingredient("mystery", "1", "unit")])
    m = member("A", allergens={"gluten"})
    result = AllergenValidator().validate(r, [m])
    # No allergen data at all -> valid but warned.
    assert result.valid is True
    assert result.warnings


def test_dietary_hard_vegan_rejects_meat():
    r = recipe("r1", {"lunch"}, [ingredient("beef", "200", "g")], allergens={"meat"})
    m = member("A", hard={"vegan"})
    result = DietaryRestrictionValidator().validate(r, [m])
    assert result.valid is False


def test_dietary_soft_preference_penalized_not_rejected():
    r = recipe("r1", {"lunch"}, [ingredient("cilantro", "5", "g")])
    m = member("A", soft=["avoid:cilantro"])
    result = DietaryRestrictionValidator().validate(r, [m])
    assert result.valid is True
    assert result.soft_violations
