"""CLI: recipe-catalog coverage for a provider/store/scope (spec §Z) — read-only.

    python -m cestaplan_api.tools.calculate_recipe_catalog_coverage \
        --provider parsebot-alcampo --scope staging
    python -m cestaplan_api.tools.calculate_recipe_catalog_coverage \
        --provider parsebot-carrefour --scope staging

Answers "what fraction of recipes can be costed with this provider's data?" — never "how many
products were imported". Writes nothing; imports nothing; a ten-product sample yields a low,
honest coverage (a chain is NEVER declared plan-ready on the strength of a small sample).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from cestaplan_api.db import SessionLocal
from cestaplan_api.services.recipe_catalog_coverage import evaluate_recipe_catalog_coverage


def run(
    provider: str,
    *,
    scope: str,
    store_id: int | None,
    postal_code: str | None,
    meal_type: str | None,
    diet: str | None,
    recipe_limit: int | None,
    output: str | None,
) -> int:
    with SessionLocal() as db:
        cov = evaluate_recipe_catalog_coverage(
            db, provider, scope=scope, store_id=store_id, postal_code=postal_code,
            meal_type=meal_type, diet=diet, recipe_limit=recipe_limit,
        )
    payload = cov.as_dict()
    if output:
        Path(output).write_text(json.dumps(payload, indent=2, ensure_ascii=False), "utf-8")
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    print(
        f"\nCadena {cov.retailer_slug} ({cov.provider_code}) · scope={scope} · "
        f"recetas={cov.total_recipes}"
    )
    print(
        f"  totalmente calculables : {cov.fully_costable_recipes}\n"
        f"  parcialmente calculables: {cov.partially_costable_recipes}\n"
        f"  no calculables          : {cov.uncostable_recipes}\n"
        f"  ingredientes sin mapear : {cov.unmapped_ingredients}/{cov.total_recipe_ingredients}\n"
        f"  sin envase resoluble    : {cov.ingredients_with_unresolved_package}\n"
        f"  sin precio              : {cov.ingredients_without_price}\n"
        f"  ámbito incompatible     : {cov.incompatible_scope_ingredients}\n"
        f"  cobertura de costeo     : {cov.costing_coverage}"
    )
    if cov.priority_unmapped_ingredients:
        print("  mapeos prioritarios     :")
        for item in cov.priority_unmapped_ingredients[:8]:
            print(f"    - {item['canonical_name']} (bloquea {item['recipes_blocked']})")
    if cov.deficit_categories:
        print("  categorías deficitarias :", [c["category"] for c in cov.deficit_categories])
    print("  próximos pasos          :")
    for step in cov.next_steps:
        print(f"    - {step}")
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description="Cobertura del recetario para un proveedor.")
    p.add_argument("--provider", required=True)
    p.add_argument("--scope", default="staging", choices=("staging", "production"))
    p.add_argument("--store-id", type=int, default=None)
    p.add_argument("--postal-code", default=None)
    p.add_argument("--meal-type", default=None)
    p.add_argument("--diet", default=None)
    p.add_argument("--recipe-limit", type=int, default=None)
    p.add_argument("--output", default=None)
    a = p.parse_args()
    raise SystemExit(
        run(
            a.provider, scope=a.scope, store_id=a.store_id, postal_code=a.postal_code,
            meal_type=a.meal_type, diet=a.diet, recipe_limit=a.recipe_limit, output=a.output,
        )
    )


if __name__ == "__main__":
    main()
