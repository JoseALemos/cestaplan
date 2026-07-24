"""Import REAL Spanish recipes from the ``belenarbizu/recetas-espanolas`` dataset.

The dataset gives real recipes (name, ingredient NAMES, portions, meal type, time) but no
quantities. Per explicit operator instruction, OpenAI ESTIMATES a realistic quantity + unit per
ingredient for the recipe's portions (extraction/estimation over a real recipe — never a synthetic
recipe). Recipes are marked ``origin=imported``, ``is_synthetic=False``, ``is_public=True``.

Runs on-platform (the OpenAI key is a Railway secret; the dataset is fetched over HTTPS; writes go
to the internal DB). Idempotent (skips a recipe whose title is already imported), bounded by
``--limit``, with ``--dry-run`` / ``--apply``. It seeds NO products or prices and never activates
any provider.

    python -m cestaplan_api.tools.import_recipes_es --dry-run --limit 5
    python -m cestaplan_api.tools.import_recipes_es --apply --limit 109
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from cestaplan_api.config import get_settings
from cestaplan_api.db import SessionLocal
from cestaplan_api.models import Ingredient, Recipe, RecipeIngredient

_DATASET = "belenarbizu/recetas-espanolas"
_ROWS_URL = (
    "https://datasets-server.huggingface.co/rows"
    "?dataset=belenarbizu%2Frecetas-espanolas&config=default&split=train&offset={off}&length={n}"
)
# belenarbizu meal-type tags -> CestaPlan meal types.
_MEAL_MAP = {
    "desayuno": "breakfast",
    "almuerzo": "lunch",
    "comida": "lunch",
    "cena": "dinner",
    "merienda": "snack",
    "aperitivo": "snack",
}
_ALLOWED_UNITS = {"g", "kg", "ml", "l", "unidad", "cucharada", "cucharadita", "taza", "pizca"}

_SYSTEM = (
    "Eres un asistente de cocina español. Recibes una receta real (nombre, lista de ingredientes "
    "por NOMBRE, y porciones). Para CADA ingrediente ESTIMA una cantidad y unidad realistas para "
    "ESAS porciones. Normaliza el nombre a un ingrediente canónico en español, singular y de "
    "supermercado (p.ej. 'huevos'->'huevo', 'patatas'->'patata'). Devuelve SOLO JSON: "
    '{"ingredients":[{"canonical":<str>,"display":<str>,"quantity":<número>,"unit":<unidad>}]}. '
    "unit debe ser una de: g, kg, ml, l, unidad, cucharada, cucharadita, taza, pizca. "
    "quantity es un número > 0. No añadas ingredientes que no estén en la lista."
)


def _fetch_recipes(limit: int) -> list[dict]:
    out: list[dict] = []
    off = 0
    while len(out) < limit:
        n = min(100, limit - len(out))
        with urllib.request.urlopen(_ROWS_URL.format(off=off, n=n)) as resp:
            page = json.load(resp)
        rows = [r["row"] for r in page.get("rows", [])]
        if not rows:
            break
        out.extend(rows)
        off += len(rows)
    return out[:limit]


def _meal_types(tags: object) -> list[str]:
    result: list[str] = []
    items = tags if isinstance(tags, list) else []
    for t in items:
        mapped = _MEAL_MAP.get(str(t).strip().lower())
        if mapped and mapped not in result:
            result.append(mapped)
    return result or ["lunch", "dinner"]


def _structure(client, row: dict) -> list[dict]:
    payload = {
        "nombre": row.get("nombre"),
        "porciones": row.get("porciones") or 4,
        "ingredientes": row.get("ingredientes") or [],
    }
    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        temperature=0,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
    )
    data = json.loads(resp.choices[0].message.content or "{}")
    cleaned: list[dict] = []
    for ing in data.get("ingredients", []):
        name = str(ing.get("canonical") or "").strip().lower()
        unit = str(ing.get("unit") or "").strip().lower()
        try:
            qty = Decimal(str(ing.get("quantity")))
        except Exception:
            continue
        if not name or qty <= 0 or unit not in _ALLOWED_UNITS:
            continue
        cleaned.append(
            {
                "canonical": name,
                "display": str(ing.get("display") or name),
                "unit": unit,
                "qty": qty,
            }
        )
    return cleaned


def _get_or_create_ingredient(db: Session, cache: dict[str, Ingredient], item: dict) -> Ingredient:
    key = item["canonical"]
    if key in cache:
        return cache[key]
    ing = db.execute(
        select(Ingredient).where(Ingredient.canonical_name == key)
    ).scalar_one_or_none()
    if ing is None:
        ing = Ingredient(
            canonical_name=key, display_name=item["display"], default_unit=item["unit"],
            is_synthetic=False,
        )
        db.add(ing)
        db.flush()
    cache[key] = ing
    return ing


def run(*, apply: bool, limit: int) -> dict:
    settings = get_settings()
    if not settings.openai_api_key:
        raise SystemExit("OPENAI_API_KEY is required (set it as a Railway secret).")
    from openai import OpenAI

    client = OpenAI(api_key=settings.openai_api_key)
    rows = _fetch_recipes(limit)

    created_recipes = 0
    skipped = 0
    created_ingredients = 0
    with SessionLocal() as db:
        ing_cache: dict[str, Ingredient] = {}
        before_ing = {i.canonical_name for i in db.execute(select(Ingredient)).scalars()}
        for row in rows:
            title = (row.get("nombre") or "").strip()
            if not title:
                continue
            exists = db.execute(
                select(Recipe).where(Recipe.title == title, Recipe.origin == "imported")
            ).scalar_one_or_none()
            if exists is not None:
                skipped += 1
                continue
            structured = _structure(client, row)
            if not structured:
                skipped += 1
                continue
            recipe = Recipe(
                origin="imported",
                is_public=True,
                is_synthetic=False,
                title=title,
                servings=int(row.get("porciones") or 4),
                meal_types=_meal_types(row.get("tipo_comida")),
                preparation_minutes=row.get("tiempo_minutos"),
            )
            db.add(recipe)
            db.flush()
            for item in structured:
                ing = _get_or_create_ingredient(db, ing_cache, item)
                db.add(
                    RecipeIngredient(
                        recipe_id=recipe.id,
                        ingredient_id=ing.id,
                        canonical_name=ing.canonical_name,
                        display_name=item["display"],
                        quantity=item["qty"],
                        unit=item["unit"],
                        optional=False,
                    )
                )
            created_recipes += 1
        after_ing = {i.canonical_name for i in db.execute(select(Ingredient)).scalars()}
        created_ingredients = len(after_ing - before_ing)
        if apply:
            db.commit()
        else:
            db.rollback()
    return {
        "mode": "apply" if apply else "dry-run",
        "dataset": _DATASET,
        "fetched": len(rows),
        "recipes_created": created_recipes,
        "recipes_skipped": skipped,
        "ingredients_created": created_ingredients,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    parser.add_argument("--limit", type=int, default=109)
    args = parser.parse_args(argv)
    result = run(apply=bool(args.apply), limit=args.limit)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
