"""CLI: run a per-provider shadow evaluation (spec §AA) — staging data only, never production.

    python -m cestaplan_api.jobs.run_price_provider_shadow \
        --provider parsebot-alcampo --recipe-limit 20
    python -m cestaplan_api.jobs.run_price_provider_shadow \
        --provider parsebot-carrefour --recipe-limit 20

Uses only staging data + synthetic/dev recipes; makes NO additional external calls when staging
data already exists; compares against the demo baseline; never modifies production.
"""

from __future__ import annotations

import argparse
import json

from cestaplan_api.db import SessionLocal
from cestaplan_api.services.provider_shadow import run_provider_shadow


def run(provider: str, recipe_limit: int, baseline: str) -> int:
    with SessionLocal() as db:
        result = run_provider_shadow(
            db, provider, recipe_limit=recipe_limit, baseline_provider=baseline
        )
        summary = {
            "provider_code": result.provider_code,
            "retailer_slug": result.retailer_slug,
            "status": result.status,
            "recipes_evaluated": result.recipes_evaluated,
            "recipes_costable": result.recipes_costable,
            "basket_known_cost": str(result.basket_known_cost),
            "baseline_provider": result.baseline_provider,
            "baseline_cost": str(result.baseline_cost),
            "absolute_difference": str(result.absolute_difference),
            "percentage_difference": (
                None if result.percentage_difference is None else str(result.percentage_difference)
            ),
            "missing_products": result.missing_products,
            "unresolved_packages": result.unresolved_packages,
            "stale_prices": result.stale_prices,
            "warnings": result.warnings,
        }
        db.commit()
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    print("\nSombra: NO productiva. Producción intacta. Datos staging/shadow no contaminan planes.")
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description="Evaluación en modo sombra de un proveedor.")
    p.add_argument("--provider", required=True)
    p.add_argument("--recipe-limit", type=int, default=20)
    p.add_argument("--baseline", default="demo")
    a = p.parse_args()
    raise SystemExit(run(a.provider, a.recipe_limit, a.baseline))


if __name__ == "__main__":
    main()
