"""apply_recipe_steps: writes reviewed steps from JSON, skipping recipes that already have them."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from cestaplan_api.models import Recipe, RecipeStep
from cestaplan_api.tools.apply_recipe_steps import run


def _recipe(db: Session, title: str, *, with_steps: bool) -> Recipe:
    r = Recipe(
        household_id=None, origin="imported", is_public=True, is_synthetic=False,
        title=title, servings=4, verification_status="verified",
    )
    db.add(r)
    db.flush()
    if with_steps:
        db.add(RecipeStep(recipe_id=r.id, step_number=1, instruction="Ya tenía."))
        db.flush()
    return r


def _write(tmp_path: Path, mapping: dict) -> Path:
    p = tmp_path / "steps.json"
    p.write_text(json.dumps(mapping), encoding="utf-8")
    return p


def test_applies_steps_and_skips_existing(db_session: Session, tmp_path: Path) -> None:
    fresh = _recipe(db_session, "ZZZ receta nueva", with_steps=False)
    has = _recipe(db_session, "ZZZ ya con pasos", with_steps=True)
    path = _write(tmp_path, {
        str(fresh.public_id): ["Paso uno.", "Paso dos.", "Paso tres."],
        str(has.public_id): ["No debería sobreescribir."],
        str(uuid.uuid4()): ["receta inexistente"],
    })

    dry = run(db_session, path=path, commit=False)
    assert dry["applied"] == 1 and dry["skipped_existing"] == 1 and dry["unknown_ids"] == 1
    assert not db_session.execute(
        select(RecipeStep).where(RecipeStep.recipe_id == fresh.id)
    ).scalars().all()  # dry-run wrote nothing

    run(db_session, path=path, commit=True)
    steps = db_session.execute(
        select(RecipeStep).where(RecipeStep.recipe_id == fresh.id).order_by(RecipeStep.step_number)
    ).scalars().all()
    assert [s.instruction for s in steps] == ["Paso uno.", "Paso dos.", "Paso tres."]
    # The recipe that already had steps keeps its single original step.
    kept = db_session.execute(
        select(RecipeStep).where(RecipeStep.recipe_id == has.id)
    ).scalars().all()
    assert len(kept) == 1 and kept[0].instruction == "Ya tenía."
