"""Idempotent backfill of provenance / verification metadata for the imported recipes.

The 100 recipes imported from ``belenarbizu/recetas-espanolas`` (``origin=imported``) had their
per-ingredient quantities ESTIMATED by an LLM (the dataset carried ingredient names only). This
tool records that provenance WITHOUT touching any recipe content or quantity value:

  * Recipe: source_dataset / source_reference / source_license / imported_at (= created_at) /
    verification_status=pending_review / estimation_model / estimation_prompt_version;
  * RecipeIngredient: quantity_source=ai_estimated / verification_status=pending_review.

An AI-estimated quantity is therefore NEVER marked verified. Fill-only + idempotent: it only sets
fields still NULL, so a second run makes no change. Dry-run shows the counts without writing.

    python -m cestaplan_api.tools.backfill_recipe_provenance --dry-run
    python -m cestaplan_api.tools.backfill_recipe_provenance --apply
"""

from __future__ import annotations

import argparse
import json
import sys

from sqlalchemy import select
from sqlalchemy.orm import Session

from cestaplan_api.db import SessionLocal
from cestaplan_api.models import Recipe, RecipeIngredient

SOURCE_DATASET = "belenarbizu/recetas-espanolas"
SOURCE_REFERENCE = "https://huggingface.co/datasets/belenarbizu/recetas-espanolas"
# The dataset's licence is not asserted on its card; left unrecorded rather than fabricated.
SOURCE_LICENSE: str | None = None
ESTIMATION_MODEL = "gpt-4o-mini"
ESTIMATION_PROMPT_VERSION = "belenarbizu-quantities-v1"


def backfill(db: Session) -> dict[str, int]:
    """Fill-only provenance for ``origin=imported`` recipes. Returns the counts changed."""
    recipes_updated = 0
    ingredients_updated = 0
    imported = db.execute(
        select(Recipe).where(Recipe.origin == "imported", Recipe.source_dataset.is_(None))
    ).scalars().all()
    for recipe in imported:
        recipe.source_dataset = SOURCE_DATASET
        recipe.source_reference = SOURCE_REFERENCE
        recipe.source_license = SOURCE_LICENSE
        recipe.imported_at = recipe.created_at
        recipe.verification_status = "pending_review"
        recipe.estimation_model = ESTIMATION_MODEL
        recipe.estimation_prompt_version = ESTIMATION_PROMPT_VERSION
        recipes_updated += 1

    # Ingredient quantities of imported recipes were AI-estimated (never verified).
    imported_recipe_ids = select(Recipe.id).where(Recipe.origin == "imported")
    rows = db.execute(
        select(RecipeIngredient).where(
            RecipeIngredient.recipe_id.in_(imported_recipe_ids),
            RecipeIngredient.quantity_source.is_(None),
        )
    ).scalars().all()
    for ri in rows:
        ri.quantity_source = "ai_estimated"
        ri.verification_status = "pending_review"
        ingredients_updated += 1

    db.flush()
    return {"recipes_updated": recipes_updated, "ingredients_updated": ingredients_updated}


def run(*, apply: bool) -> dict[str, object]:
    with SessionLocal() as db:
        result = backfill(db)
        if apply:
            db.commit()
        else:
            db.rollback()
    return {"mode": "apply" if apply else "dry-run", **result}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    print(json.dumps(run(apply=bool(args.apply)), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
