"""Enrich existing real Open-Prices products from Open Food Facts (data only, never prices).

The Open Prices sync creates real ``Product`` rows with real barcodes; this backfills the OFF
"data nutrition" (nutrition per 100 g, allergens, brand, image, category) for the ones still
missing it. It reuses the enrichment service, is idempotent, and degrades gracefully on an OFF
404 / network error (the product simply stays un-enriched). It never reads or writes a price.

Run::

    python -m cestaplan_api.scripts.enrich_open_prices --all
    python -m cestaplan_api.scripts.enrich_open_prices --store <store_public_id>
    uv run python -m cestaplan_api.scripts.enrich_open_prices --all
"""

from __future__ import annotations

import argparse
import sys
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from cestaplan_api.adapters.openprices import OP_ADAPTER_KEY
from cestaplan_api.db import SessionLocal
from cestaplan_api.models import (
    Product,
    ProductNutrition,
    ProductPrice,
    Retailer,
    Store,
)
from cestaplan_api.services.enrichment import off_source_enabled
from cestaplan_api.services.open_prices_sync import enrich_products


def _op_product_ids_all(db: Session) -> list[int]:
    """Real Open-Prices product ids that still lack a ProductNutrition row."""
    return list(
        db.execute(
            select(Product.id)
            .join(Retailer, Retailer.id == Product.retailer_id)
            .outerjoin(ProductNutrition, ProductNutrition.product_id == Product.id)
            .where(
                Retailer.adapter_key == OP_ADAPTER_KEY,
                Product.deleted_at.is_(None),
                ProductNutrition.id.is_(None),
            )
        ).scalars().all()
    )


def _op_product_ids_for_store(db: Session, store: Store) -> list[int]:
    """Product ids with at least one price observed in ``store`` and no nutrition yet."""
    return list(
        db.execute(
            select(Product.id)
            .join(ProductPrice, ProductPrice.product_id == Product.id)
            .outerjoin(ProductNutrition, ProductNutrition.product_id == Product.id)
            .where(
                ProductPrice.store_id == store.id,
                Product.deleted_at.is_(None),
                ProductNutrition.id.is_(None),
            )
            .distinct()
        ).scalars().all()
    )


def run(*, all_products: bool, store_public_id: str | None) -> int:
    with SessionLocal() as session:
        if not off_source_enabled(session):
            session.commit()
            print("La fuente Open Food Facts está deshabilitada; nada que enriquecer.")
            return 0

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
            product_ids = _op_product_ids_for_store(session, store)
        else:
            product_ids = _op_product_ids_all(session)

        enriched = enrich_products(session, product_ids, skip_enriched=True)
        session.commit()

    print(
        f"Open Food Facts enrichment — {len(product_ids)} producto(s) candidatos, "
        f"{enriched} enriquecido(s) con datos OFF."
    )
    print(
        "Datos de Open Food Facts, disponibles bajo Open Database License (ODbL) 1.0. "
        "https://openfoodfacts.org"
    )
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Enriquece productos reales de Open Prices con datos de Open Food Facts."
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--all", action="store_true", help="Enriquece todos los productos de Open Prices."
    )
    group.add_argument(
        "--store", metavar="PUBLIC_ID", help="Enriquece los productos de una sola tienda."
    )
    args = parser.parse_args()
    raise SystemExit(run(all_products=args.all, store_public_id=args.store))


if __name__ == "__main__":
    main()
