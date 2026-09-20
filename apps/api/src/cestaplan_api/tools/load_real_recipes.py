"""Idempotent loader for AI-authored real recipes (behind a human review gate).

Reads a recipe JSON file (default: ``data/recipes/ai_batch_01.json``) and *get-or-creates* each
recipe by its natural key ``(is_synthetic=False, title)``. Every recipe is inserted as a REVIEW
DRAFT — ``is_public=False`` and ``verification_status="pending_review"`` — so it is NOT served on
the live plan rail (which filters ``is_public=True AND deleted_at IS NULL``) until a human approves
it. Nothing is ever overwritten: a title that already belongs to a non-synthetic recipe is reused
untouched, so a second run is a no-op (``created=0``).

Provenance is stamped clearly as AI-estimated (never "verified"): ``origin="ai_generated"``,
``estimation_model``/``generated_by`` carry the model marker, and every ingredient line is
``quantity_source="ai_estimated"``.

Ingredient rows are pre-seeded; this tool NEVER creates them. Each ``canonical_name`` is resolved
to its :class:`Ingredient` via the same case-insensitive matcher used by the importer
(:func:`cestaplan_api.services.importer._match_ingredient`). A name that resolves to nothing is
reported as ``unresolved`` (never silently mapped to a wrong ingredient) and its line is skipped;
the recipe is still created with the lines that did resolve (the review gate catches the gap).

The per-recipe summary also flags, for each recipe, which MANDATORY ingredients fall outside the
26 "safe" canonical names that have deterministic real-product mapping rules — these are coverage
gaps a reviewer must confirm are costable against the target chains.

Modes::

    python -m cestaplan_api.tools.load_real_recipes [--file PATH]              # dry-run (default)
    python -m cestaplan_api.tools.load_real_recipes [--file PATH] --commit     # commit

``--dry-run`` (default) runs the full logic inside a transaction that ALWAYS rolls back and prints
a sanitized per-recipe diff. ``--commit`` commits a single transaction. This tool makes NO network
calls and needs no credentials; it works against a local/test database.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from cestaplan_api.db import SessionLocal
from cestaplan_api.models import Recipe, RecipeIngredient, RecipeStep
from cestaplan_api.services.equipment_normalize import normalize_equipment
from cestaplan_api.services.importer import _match_ingredient

# Marker recorded on every recipe so the provenance is unambiguously AI-estimated (not verified).
DEFAULT_MODEL_MARKER = "claude-opus-4-8"

# The 26 "safe" canonical names: mandatory ingredients within this set have deterministic
# real-product mapping rules, so a recipe whose mandatory ingredients are all here is
# fully-costable-ready. Mandatory ingredients outside it are allowed but reported as coverage gaps.
SAFE_26: frozenset[str] = frozenset({
    "tomate", "cebolla", "ajo", "pimiento_rojo", "calabacin", "zanahoria", "espinaca", "patata",
    "platano", "limon", "arandano", "chorizo", "bacalao", "leche_entera", "leche_desnatada",
    "yogur_natural", "mantequilla", "avena_copos", "aceite_oliva", "sal", "pimenton", "comino",
    "vinagre", "almendra", "aceitunas", "azucar",
})

# data/recipes/ai_batch_01.json relative to the apps/api package root.
DEFAULT_RECIPES_PATH = Path(__file__).resolve().parents[3] / "data" / "recipes" / "ai_batch_01.json"


class RecipeLoadError(RuntimeError):
    """A fail-closed load failure carrying a stable ``code``."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(code)


@dataclass(slots=True)
class RecipeResult:
    title: str
    created: bool
    ingredients_created: int
    steps_created: int
    coverage_gaps: list[str] = field(default_factory=list)  # mandatory names outside SAFE_26
    unresolved: list[str] = field(default_factory=list)  # canonical_names matching no Ingredient

    def as_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "created": self.created,
            "ingredients_created": self.ingredients_created,
            "steps_created": self.steps_created,
            "coverage_gaps": sorted(self.coverage_gaps),
            "unresolved": sorted(self.unresolved),
        }


@dataclass(slots=True)
class LoadDiff:
    recipes_created: int = 0
    recipes_reused: int = 0
    per_recipe: list[RecipeResult] = field(default_factory=list)

    @property
    def fully_costable_ready(self) -> int:
        return sum(1 for r in self.per_recipe if not r.coverage_gaps)

    @property
    def with_gaps(self) -> int:
        return sum(1 for r in self.per_recipe if r.coverage_gaps)

    def as_dict(self) -> dict[str, Any]:
        return {
            "recipes_created": self.recipes_created,
            "recipes_reused": self.recipes_reused,
            "fully_costable_ready": self.fully_costable_ready,
            "with_coverage_gaps": self.with_gaps,
            "per_recipe": [r.as_dict() for r in self.per_recipe],
        }


def _d(value: object) -> Decimal:
    return Decimal(str(value))


def load_recipes(session: Session, recipes: list[dict[str, Any]], *,
                 model_marker: str = DEFAULT_MODEL_MARKER,
                 source_dataset: str | None = None) -> LoadDiff:
    """Get-or-create every recipe additively. Caller controls commit/rollback."""
    diff = LoadDiff()
    now = datetime.now(UTC)
    for spec in recipes:
        diff.per_recipe.append(
            _load_one(session, spec, now=now, model_marker=model_marker,
                      source_dataset=source_dataset, diff=diff)
        )
    session.flush()
    return diff


def _load_one(session: Session, spec: dict[str, Any], *, now: datetime, model_marker: str,
              source_dataset: str | None, diff: LoadDiff) -> RecipeResult:
    title = spec["title"]
    ingredients = spec.get("ingredients", [])
    coverage_gaps = [
        ing["canonical_name"] for ing in ingredients
        if not ing.get("optional", False) and ing["canonical_name"] not in SAFE_26
    ]

    existing = session.execute(
        select(Recipe).where(Recipe.title == title, Recipe.is_synthetic.is_(False))
    ).scalars().all()
    if len(existing) > 1:
        raise RecipeLoadError("recipe_identity_ambiguous", title)
    if existing:
        diff.recipes_reused += 1
        return RecipeResult(title=title, created=False, ingredients_created=0, steps_created=0,
                            coverage_gaps=coverage_gaps)

    recipe = Recipe(
        household_id=None,
        origin="ai_generated",
        is_public=False,  # REVIEW GATE: not served on the live rail until a human approves it.
        is_synthetic=False,
        title=title,
        description=spec.get("description"),
        servings=int(spec["servings"]),
        meal_types=list(spec.get("meal_types") or []) or None,
        cuisine=spec.get("cuisine"),
        preference_tags=list(spec.get("preference_tags") or []) or None,
        preparation_minutes=spec.get("preparation_minutes"),
        cooking_minutes=spec.get("cooking_minutes"),
        # Normalize free-text equipment (olla, sarten, horno…) to canonical codes so the
        # engine's subset filter can actually match; utensils/unknowns impose no need.
        required_equipment=normalize_equipment(spec.get("required_equipment")) or None,
        leftover_reuse=spec.get("leftover_reuse"),
        storage_instructions=spec.get("storage_instructions"),
        reheating_instructions=spec.get("reheating_instructions"),
        generated_by=model_marker,
        estimation_model=model_marker,
        verification_status="pending_review",
        source_dataset=source_dataset,
        imported_at=now,
    )
    session.add(recipe)
    session.flush()

    unresolved: list[str] = []
    ingredients_created = 0
    for ing in ingredients:
        canonical_name = ing["canonical_name"]
        matched = _match_ingredient(session, canonical_name)
        if matched is None:
            # Never silently map to a wrong ingredient: report and skip this line.
            unresolved.append(canonical_name)
            continue
        session.add(RecipeIngredient(
            recipe_id=recipe.id,
            ingredient_id=matched.id,
            canonical_name=canonical_name,
            display_name=ing.get("display_name") or matched.display_name,
            quantity=_d(ing["quantity"]),
            unit=ing["unit"],
            optional=bool(ing.get("optional", False)),
            substitution_group=ing.get("substitution_group"),
            quantity_source="ai_estimated",
            verification_status="pending_review",
        ))
        ingredients_created += 1

    steps_created = 0
    for step_number, instruction in enumerate(spec.get("steps", []), start=1):
        session.add(RecipeStep(
            recipe_id=recipe.id, step_number=step_number, instruction=instruction,
            duration_minutes=None,
        ))
        steps_created += 1

    diff.recipes_created += 1
    return RecipeResult(title=title, created=True, ingredients_created=ingredients_created,
                        steps_created=steps_created, coverage_gaps=coverage_gaps,
                        unresolved=unresolved)


def _read_recipes(path: Path) -> list[dict[str, Any]]:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RecipeLoadError("recipe_file_unreadable", str(path)) from exc
    data = json.loads(raw)
    if not isinstance(data, list):
        raise RecipeLoadError("recipe_file_not_a_list", str(path))
    return data


def run(*, path: Path, commit: bool, model_marker: str = DEFAULT_MODEL_MARKER) -> dict[str, Any]:
    """Execute the load in ONE transaction. dry-run rolls back; commit persists."""
    recipes = _read_recipes(path)
    session = SessionLocal()
    try:
        diff = load_recipes(session, recipes, model_marker=model_marker,
                            source_dataset=path.stem)
        result: dict[str, Any] = {
            "mode": "commit" if commit else "dry-run",
            "file": str(path),
            "recipe_count": len(recipes),
            "diff": diff.as_dict(),
        }
        if commit:
            session.commit()
            result["committed"] = True
        else:
            session.rollback()
            result["committed"] = False
        return result
    except RecipeLoadError as exc:
        session.rollback()
        return {"mode": "commit" if commit else "dry-run", "file": str(path),
                "committed": False, "error": exc.code, "detail": exc.detail}
    finally:
        session.close()


def _print_summary(result: dict[str, Any]) -> None:
    json.dump(result, sys.stdout, indent=2, ensure_ascii=False, sort_keys=True, default=str)
    sys.stdout.write("\n")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--file", type=Path, default=DEFAULT_RECIPES_PATH,
                   help="recipe JSON file (default: data/recipes/ai_batch_01.json)")
    p.add_argument("--model-marker", default=DEFAULT_MODEL_MARKER,
                   help="value stamped on estimation_model/generated_by")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="run + rollback (default)")
    mode.add_argument("--commit", action="store_true", help="run + commit in one transaction")
    a = p.parse_args(argv)
    result = run(path=a.file, commit=bool(a.commit), model_marker=a.model_marker)
    _print_summary(result)
    return 0 if result.get("error") is None else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
