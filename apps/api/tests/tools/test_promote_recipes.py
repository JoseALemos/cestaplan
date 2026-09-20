"""promote_recipes: pending_review -> verified+public, skipping stepless recipes by default."""

from __future__ import annotations

from sqlalchemy.orm import Session

from cestaplan_api.models import Recipe, RecipeStep
from cestaplan_api.tools.promote_recipes import run


def _pending(db: Session, title: str, *, with_steps: bool) -> Recipe:
    recipe = Recipe(
        household_id=None,
        origin="imported",
        is_public=False,
        is_synthetic=False,
        title=title,
        servings=2,
        verification_status="pending_review",
    )
    db.add(recipe)
    db.flush()
    if with_steps:
        db.add(RecipeStep(recipe_id=recipe.id, step_number=1, instruction="Cocinar."))
        db.flush()
    return recipe


def test_promotes_only_recipes_with_steps_by_default(db_session: Session) -> None:
    stepped = _pending(db_session, "ZZZ con pasos", with_steps=True)
    stepless = _pending(db_session, "ZZZ sin pasos", with_steps=False)

    dry = run(db_session, commit=False, allow_stepless=False)
    assert dry["promoted"] == 1 and dry["skipped"] == 1
    # Dry-run changes nothing.
    assert stepped.is_public is False and stepped.verification_status == "pending_review"

    result = run(db_session, commit=True, allow_stepless=False)
    assert result["promoted"] == 1 and result["skipped"] == 1
    db_session.refresh(stepped)
    db_session.refresh(stepless)
    assert stepped.is_public is True and stepped.verification_status == "verified"
    # Stepless stays gated (not published without instructions).
    assert stepless.is_public is False and stepless.verification_status == "pending_review"


def test_allow_stepless_promotes_everything(db_session: Session) -> None:
    stepless = _pending(db_session, "ZZZ sin pasos 2", with_steps=False)
    result = run(db_session, commit=True, allow_stepless=True)
    assert result["promoted"] >= 1
    db_session.refresh(stepless)
    assert stepless.is_public is True and stepless.verification_status == "verified"
