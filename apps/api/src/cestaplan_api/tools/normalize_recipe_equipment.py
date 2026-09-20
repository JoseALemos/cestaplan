"""One-off data fix: normalize recipes' ``required_equipment`` to canonical codes.

Recipes imported from ``data/recipes/*.json`` stored free-text Spanish equipment terms
(``olla``, ``sarten``, even ``horno`` = ``oven``). The engine filters candidates with a
strict subset check, so those recipes were silently excluded from EVERY plan. This tool
rewrites their ``required_equipment`` to the canonical :data:`KnownEquipment` codes using
:mod:`cestaplan_api.services.equipment_normalize` (the same mapping the loader now applies).

Dry-run by default (prints what would change); ``--commit`` persists. If any term is
neither canonical, mapped, nor a recognised utensil it is reported as UNMAPPED so the
mapping can be extended before committing — the fix never silently drops a real appliance.

    python -m cestaplan_api.tools.normalize_recipe_equipment            # review
    python -m cestaplan_api.tools.normalize_recipe_equipment --commit   # apply
"""

from __future__ import annotations

import argparse
import sys

from sqlalchemy import select
from sqlalchemy.orm import Session

from cestaplan_api.db import SessionLocal
from cestaplan_api.models import Recipe
from cestaplan_api.services.equipment_normalize import normalize_equipment, unmapped_terms


def run(session: Session, *, commit: bool) -> dict[str, object]:
    recipes = (
        session.execute(select(Recipe).where(Recipe.deleted_at.is_(None)))
        .scalars()
        .all()
    )
    changed: list[dict[str, object]] = []
    unmapped_all: dict[str, int] = {}
    for recipe in recipes:
        before = list(recipe.required_equipment or [])
        after = normalize_equipment(before)
        for term in unmapped_terms(before):
            unmapped_all[term] = unmapped_all.get(term, 0) + 1
        if before == after:
            continue  # already canonical (seed recipes) — nothing to do
        changed.append({"title": recipe.title, "before": before, "after": after})
        if commit:
            recipe.required_equipment = after or None

    if commit:
        session.commit()

    return {
        "scanned": len(recipes),
        "changed": len(changed),
        "committed": commit,
        "unmapped": unmapped_all,
        "details": changed,
    }


def _print_summary(result: dict[str, object]) -> None:
    print(f"Recetas revisadas: {result['scanned']}")
    print(f"Recetas a normalizar: {result['changed']}")
    for item in result["details"]:  # type: ignore[index]
        print(f"  · {item['title']}: {item['before']} -> {item['after']}")
    unmapped = result["unmapped"]  # type: ignore[assignment]
    if unmapped:
        print("\n⚠️  Términos SIN MAPEAR (revisa el mapa antes de --commit):")
        for term, count in sorted(unmapped.items(), key=lambda kv: -kv[1]):  # type: ignore[union-attr]
            print(f"    {term}  (x{count})")
    print("\nAPLICADO (commit)" if result["committed"] else "\nDRY-RUN (sin cambios; usa --commit)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commit", action="store_true", help="Persistir los cambios.")
    args = parser.parse_args(argv)

    session = SessionLocal()
    try:
        result = run(session, commit=args.commit)
    finally:
        session.close()
    _print_summary(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
