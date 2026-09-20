"""Apply reviewed, AI-drafted preparation steps to recipes that lack them.

Steps are drafted by AI and REVIEWED by a human before landing here; this tool only writes
them. Input is a JSON object mapping a recipe ``public_id`` to an ordered list of step
strings, committed at ``apps/api/data/recipes/generated_steps.json`` so the diff itself is
the review record.

SAFETY: a recipe that already has steps is skipped (never duplicate/overwrite existing
content), and an unknown/mismatched id is reported, never silently ignored. Dry-run by
default.

    python -m cestaplan_api.tools.apply_recipe_steps            # dry-run review (default)
    python -m cestaplan_api.tools.apply_recipe_steps --commit   # write the steps
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from cestaplan_api.db import SessionLocal
from cestaplan_api.models import Recipe, RecipeStep

_DEFAULT_PATH = (
    Path(__file__).resolve().parents[2] / "data" / "recipes" / "generated_steps.json"
)


def run(session: Session, *, path: Path, commit: bool) -> dict[str, object]:
    data: dict[str, list[str]] = json.loads(path.read_text(encoding="utf-8"))
    applied: list[str] = []
    skipped_existing: list[str] = []
    unknown: list[str] = []
    empty: list[str] = []

    for public_id, steps in data.items():
        recipe = session.execute(
            select(Recipe).where(Recipe.public_id == uuid.UUID(public_id))
        ).scalar_one_or_none()
        if recipe is None or recipe.deleted_at is not None:
            unknown.append(public_id)
            continue
        if not steps or not all(isinstance(s, str) and s.strip() for s in steps):
            empty.append(recipe.title)
            continue
        if recipe.steps:
            skipped_existing.append(recipe.title)
            continue
        applied.append(recipe.title)
        if commit:
            for i, instruction in enumerate(steps, start=1):
                session.add(
                    RecipeStep(
                        recipe_id=recipe.id,
                        step_number=i,
                        instruction=instruction.strip(),
                    )
                )

    if commit:
        session.commit()

    return {
        "in_file": len(data),
        "applied": len(applied),
        "skipped_existing": len(skipped_existing),
        "unknown_ids": len(unknown),
        "empty": len(empty),
        "committed": commit,
        "applied_titles": applied,
        "skipped_titles": skipped_existing,
        "unknown_id_list": unknown,
        "empty_titles": empty,
    }


def _print_summary(result: dict[str, object]) -> None:
    print(f"Entradas en el fichero: {result['in_file']}")
    print(
        f"A aplicar: {result['applied']} | ya con pasos: {result['skipped_existing']} | "
        f"ids desconocidos: {result['unknown_ids']} | vacías: {result['empty']}"
    )
    if result["unknown_id_list"]:
        print("\n⚠️  ids no encontrados:")
        for pid in result["unknown_id_list"]:  # type: ignore[union-attr]
            print(f"  · {pid}")
    if result["empty_titles"]:
        print("\n⚠️  entradas vacías/ inválidas:")
        for t in result["empty_titles"]:  # type: ignore[union-attr]
            print(f"  · {t}")
    print("\nAPLICADO (commit)" if result["committed"] else "\nDRY-RUN (sin cambios; usa --commit)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commit", action="store_true", help="Persistir los pasos.")
    parser.add_argument("--file", type=Path, default=_DEFAULT_PATH, help="JSON de pasos.")
    args = parser.parse_args(argv)

    session = SessionLocal()
    try:
        result = run(session, path=args.file, commit=args.commit)
    finally:
        session.close()
    _print_summary(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
