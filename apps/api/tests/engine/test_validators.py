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


# --- diet enforcement by ingredient CATEGORY (real recipes carry no "meat" allergen) ------------
def test_vegan_rejects_meat_by_category_not_declared_allergen():
    # A real chicken dish: canonical name "pollo_pechuga", category "carne", NO declared allergen.
    r = recipe("r1", {"lunch"}, [ingredient("pollo_pechuga", "200", "g", category="carne")])
    result = DietaryRestrictionValidator().validate(r, [member("A", hard={"vegan"})])
    assert result.valid is False
    assert any("vegan" in v for v in result.hard_violations)


def test_spanish_diet_values_are_enforced():
    meat = recipe("r1", {"lunch"}, [ingredient("cerdo_lomo", "200", "g", category="carne")])
    for diet in ("vegano", "vegetariano", "pescetariano"):
        result = DietaryRestrictionValidator().validate(meat, [member("A", hard={diet})])
        assert result.valid is False, f"{diet} should exclude a meat dish"


def test_vegetarian_excludes_fish_but_allows_eggs_and_dairy():
    fish = recipe("f", {"lunch"}, [ingredient("salmon", "150", "g", category="pescado_marisco")])
    eggs = recipe("e", {"breakfast"}, [ingredient("huevo", "2", "unit", category="huevos")])
    dairy = recipe("d", {"breakfast"}, [ingredient("yogur", "125", "g", category="lacteos")])
    veg = member("A", hard={"vegetariano"})
    assert DietaryRestrictionValidator().validate(fish, [veg]).valid is False
    assert DietaryRestrictionValidator().validate(eggs, [veg]).valid is True
    assert DietaryRestrictionValidator().validate(dairy, [veg]).valid is True


def test_pescatarian_excludes_meat_but_allows_fish():
    meat = recipe("m", {"lunch"}, [ingredient("ternera_picada", "200", "g", category="carne")])
    fish = recipe("f", {"lunch"}, [ingredient("merluza", "180", "g", category="pescado_marisco")])
    peace = member("A", hard={"pescetariano"})
    assert DietaryRestrictionValidator().validate(meat, [peace]).valid is False
    assert DietaryRestrictionValidator().validate(fish, [peace]).valid is True


def test_omnivoro_is_a_noop():
    meat = recipe("m", {"lunch"}, [ingredient("pollo_pechuga", "200", "g", category="carne")])
    assert DietaryRestrictionValidator().validate(meat, [member("A", hard={"omnivoro"})]).valid


def test_vegan_recipe_passes_for_vegan():
    r = recipe(
        "v", {"lunch"},
        [
            ingredient("alubia", "300", "g", category="legumbres"),
            ingredient("zanahoria", "150", "g", category="verduras"),
        ],
    )
    assert DietaryRestrictionValidator().validate(r, [member("A", hard={"vegano"})]).valid


def test_dietary_soft_preference_penalized_not_rejected():
    r = recipe("r1", {"lunch"}, [ingredient("cilantro", "5", "g")])
    m = member("A", soft=["avoid:cilantro"])
    result = DietaryRestrictionValidator().validate(r, [m])
    assert result.valid is True
    assert result.soft_violations
