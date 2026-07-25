"""Recipe provenance backfill: records source + AI-estimation without altering any quantity, and
never marks an estimated quantity as verified. Idempotent; leaves non-imported recipes untouched."""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from cestaplan_api.models import Ingredient, Recipe, RecipeIngredient
from cestaplan_api.tools.backfill_recipe_provenance import backfill


def _imported_recipe(db: Session) -> tuple[Recipe, RecipeIngredient]:
    ing = Ingredient(canonical_name="patata_test", display_name="Patata", is_synthetic=False)
    db.add(ing)
    db.flush()
    recipe = Recipe(
        origin="imported", is_public=True, is_synthetic=False, title="Receta importada test",
        servings=4, meal_types=["lunch"],
    )
    db.add(recipe)
    db.flush()
    ri = RecipeIngredient(
        recipe_id=recipe.id, ingredient_id=ing.id, canonical_name=ing.canonical_name,
        quantity=Decimal("500"), unit="g", optional=False,
    )
    db.add(ri)
    db.flush()
    return recipe, ri


def test_backfill_marks_imported_provenance_without_touching_quantity(db_session: Session) -> None:
    recipe, ri = _imported_recipe(db_session)
    original_qty = ri.quantity

    result = backfill(db_session)
    assert result["recipes_updated"] >= 1
    assert result["ingredients_updated"] >= 1

    db_session.refresh(recipe)
    db_session.refresh(ri)
    assert recipe.source_dataset == "belenarbizu/recetas-espanolas"
    assert recipe.verification_status == "pending_review"
    assert recipe.estimation_model == "gpt-4o-mini"
    assert recipe.imported_at == recipe.created_at
    # The AI-estimated quantity is recorded as such and NEVER marked verified.
    assert ri.quantity_source == "ai_estimated"
    assert ri.verification_status == "pending_review"
    assert ri.verification_status != "verified"
    # The quantity value itself is untouched.
    assert ri.quantity == original_qty


def test_backfill_is_idempotent(db_session: Session) -> None:
    _imported_recipe(db_session)
    first = backfill(db_session)
    assert first["recipes_updated"] >= 1
    second = backfill(db_session)
    assert second == {"recipes_updated": 0, "ingredients_updated": 0}


def test_backfill_leaves_non_imported_recipes_untouched(db_session: Session) -> None:
    seed = db_session.execute(
        select(Recipe).where(Recipe.origin != "imported").order_by(Recipe.id)
    ).scalars().first()
    assert seed is not None
    backfill(db_session)
    db_session.refresh(seed)
    assert seed.source_dataset is None
    assert seed.verification_status is None
