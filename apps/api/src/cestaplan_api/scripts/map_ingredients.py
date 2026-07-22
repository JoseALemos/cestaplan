"""Map real chain-store products onto canonical ingredients (mapping command).

Matches every unmapped real (``is_synthetic=False``) product to a canonical
:class:`~cestaplan_api.models.Ingredient` where it is CLEARLY correct and inserts an
:class:`~cestaplan_api.models.IngredientProductMapping`. Conservative and idempotent: a
product already mapped is skipped, so re-running only maps freshly-synced products.

Prints the mapped count, a per-chain breakdown, and the resulting **chain-level** ingredient
coverage (pricing is by chain, not by single store): how many of the recipe's ~75 canonical
ingredients are now mapped AND priced somewhere in each chain.

Run::

    python -m cestaplan_api.scripts.map_ingredients --all
    python -m cestaplan_api.scripts.map_ingredients --store <store_public_id>
    uv run python -m cestaplan_api.scripts.map_ingredients --all
"""

from __future__ import annotations

import argparse
import sys
import uuid

from sqlalchemy import select

from cestaplan_api.db import SessionLocal
from cestaplan_api.models import Store
from cestaplan_api.services.ingredient_matching import (
    MappingSummary,
    all_chain_coverage,
    map_real_products,
)


def _print_summary(summary: MappingSummary) -> None:
    print(
        f"Mapeo de ingredientes — analizados={summary.scanned} "
        f"mapeados={summary.mapped} sin_coincidencia={summary.unmatched}"
    )
    if summary.per_chain:
        print("  Por cadena (productos mapeados):")
        for chain, count in sorted(
            summary.per_chain.items(), key=lambda kv: kv[1], reverse=True
        ):
            print(f"    {chain:<11}: {count}")
    if summary.samples:
        print("  Muestras (producto → ingrediente, confianza):")
        for s in summary.samples[:10]:
            print(
                f"    [{s['chain']:<9}] {s['product'][:42]:<42} → "
                f"{s['ingredient']:<18} ({s['confidence']})"
            )
    print("  Cobertura por cadena (ingredientes canónicos con precio real):")
    for cov in summary.chain_coverage:
        print(
            f"    {cov['chain']:<11}: {cov['priced_ingredients']}/{cov['total_ingredients']} "
            f"→ {', '.join(cov['ingredients']) or '—'}"  # type: ignore[arg-type]
        )


def run(*, store_public_id: str | None) -> int:
    with SessionLocal() as session:
        store_id: int | None = None
        if store_public_id is not None:
            try:
                pid = uuid.UUID(store_public_id)
            except ValueError:
                print(f"store id no es un UUID válido: {store_public_id!r}", file=sys.stderr)
                return 2
            store = session.execute(
                select(Store).where(Store.public_id == pid)
            ).scalar_one_or_none()
            if store is None:
                print(f"No existe la tienda {store_public_id}.", file=sys.stderr)
                return 1
            store_id = store.id

        summary = map_real_products(session, store_id=store_id)
        summary.chain_coverage = all_chain_coverage(session)
        session.commit()

    _print_summary(summary)
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Mapea productos reales de cadena a ingredientes canónicos."
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--all", action="store_true", help="Mapea todos los productos reales sin mapear."
    )
    group.add_argument(
        "--store",
        metavar="PUBLIC_ID",
        help="Mapea sólo los productos con precio en una tienda (por su id público).",
    )
    args = parser.parse_args()
    raise SystemExit(run(store_public_id=args.store))


if __name__ == "__main__":
    main()
