"""Promote reviewed recipes: pending_review -> verified + public (spec: recipe review gate).

Imported recipes land as ``verification_status="pending_review"`` and ``is_public=False``, so
they never enter plans (candidate providers require ``is_public``) nor the ``/recetas`` browse
(which lists only NULL/verified). This tool promotes them once a human has reviewed them.

SAFETY: by default a recipe WITHOUT preparation steps is skipped — publishing a recipe with
ingredients but no instructions would show a broken recipe in the browse AND put an
un-cookable meal into generated plans. ``--allow-stepless`` overrides this when the owner
explicitly accepts ingredient-only recipes.

    python -m cestaplan_api.tools.promote_recipes                 # dry-run review (default)
    python -m cestaplan_api.tools.promote_recipes --commit        # promote (steps required)
    python -m cestaplan_api.tools.promote_recipes --commit --allow-stepless
"""

from __future__ import annotations

import argparse
import sys

from sqlalchemy import select
from sqlalchemy.orm import Session

from cestaplan_api.db import SessionLocal
from cestaplan_api.models import Recipe


def run(session: Session, *, commit: bool, allow_stepless: bool) -> dict[str, object]:
    recipes = (
        session.execute(
            select(Recipe).where(
                Recipe.verification_status == "pending_review",
                Recipe.deleted_at.is_(None),
            )
        )
        .scalars()
        .all()
    )
    promoted: list[str] = []
    skipped: list[dict[str, str]] = []
    for recipe in recipes:
        has_steps = bool(recipe.steps)
        if not has_steps and not allow_stepless:
            skipped.append({"title": recipe.title, "reason": "sin pasos de preparación"})
            continue
        promoted.append(recipe.title)
        if commit:
            recipe.verification_status = "verified"
            recipe.is_public = True

    if commit:
        session.commit()

    return {
        "pending_reviewed": len(recipes),
        "promoted": len(promoted),
        "skipped": len(skipped),
        "committed": commit,
        "allow_stepless": allow_stepless,
        "skipped_detail": skipped,
        "promoted_titles": promoted,
    }


def _print_summary(result: dict[str, object]) -> None:
    print(f"Recetas pending_review: {result['pending_reviewed']}")
    print(f"A promover: {result['promoted']} | Saltadas: {result['skipped']}")
    if result["skipped"]:
        print("\nSaltadas (usa --allow-stepless para incluirlas):")
        for item in result["skipped_detail"]:  # type: ignore[union-attr]
            print(f"  · {item['title']}: {item['reason']}")
    if result["promoted"] and not result["committed"]:
        sample = result["promoted_titles"][:10]  # type: ignore[index]
        print("\nSe promoverían (muestra):")
        for title in sample:
            print(f"  · {title}")
    print("\nAPLICADO (commit)" if result["committed"] else "\nDRY-RUN (sin cambios; usa --commit)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commit", action="store_true", help="Persistir la promoción.")
    parser.add_argument(
        "--allow-stepless",
        action="store_true",
        help="Promover también recetas SIN pasos de preparación (por defecto se saltan).",
    )
    args = parser.parse_args(argv)

    session = SessionLocal()
    try:
        result = run(session, commit=args.commit, allow_stepless=args.allow_stepless)
    finally:
        session.close()
    _print_summary(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
