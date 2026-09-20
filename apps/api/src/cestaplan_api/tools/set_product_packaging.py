"""Correct the package_quantity/unit on a product's latest price (data fix).

Some crawled/community products land with ``package_quantity=1`` when they are really sold
in a pack (a dozen eggs, a 6-pack…). The engine buys WHOLE packages, so a 1-unit "package"
makes it buy N packages for N units (e.g. 11 eggs as 11 cartons -> 19,80 €). This tool sets
the correct package size on the product's most recent ``ProductPrice`` row.

Keyed by internal product id (unambiguous; this is a prod-maintenance tool run against one
database). Dry-run by default.

    python -m cestaplan_api.tools.set_product_packaging --product-ids 684,679 --package-quantity 12
    python -m cestaplan_api.tools.set_product_packaging --product-ids 684 --package-quantity 12 --commit
"""

from __future__ import annotations

import argparse
import sys
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from cestaplan_api.db import SessionLocal
from cestaplan_api.models import Product, ProductPrice


def run(
    session: Session,
    *,
    product_ids: list[int],
    package_quantity: Decimal,
    package_unit: str | None,
    commit: bool,
) -> dict[str, object]:
    changes: list[dict[str, object]] = []
    not_found: list[int] = []
    for pid in product_ids:
        product = session.get(Product, pid)
        if product is None:
            not_found.append(pid)
            continue
        price = session.execute(
            select(ProductPrice)
            .where(ProductPrice.product_id == pid)
            .order_by(ProductPrice.observed_at.desc(), ProductPrice.id.desc())
        ).scalars().first()
        if price is None:
            not_found.append(pid)
            continue
        changes.append({
            "product_id": pid,
            "name": product.name,
            "before": f"{price.package_quantity}{price.package_unit}",
            "after": f"{package_quantity}{package_unit or price.package_unit}",
        })
        if commit:
            price.package_quantity = package_quantity
            if package_unit:
                price.package_unit = package_unit

    if commit:
        session.commit()
    return {"changed": len(changes), "not_found": not_found, "committed": commit, "detail": changes}


def _print_summary(result: dict[str, object]) -> None:
    print(f"Productos a corregir: {result['changed']}")
    for c in result["detail"]:  # type: ignore[union-attr]
        print(f"  · [{c['product_id']}] {c['name']}: {c['before']} -> {c['after']}")
    if result["not_found"]:
        print(f"⚠️  no encontrados / sin precio: {result['not_found']}")
    print("\nAPLICADO (commit)" if result["committed"] else "\nDRY-RUN (sin cambios; usa --commit)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--product-ids", required=True, help="Ids internos separados por coma.")
    parser.add_argument("--package-quantity", required=True, type=Decimal)
    parser.add_argument("--package-unit", default=None)
    parser.add_argument("--commit", action="store_true")
    args = parser.parse_args(argv)
    ids = [int(x) for x in args.product_ids.split(",") if x.strip()]

    session = SessionLocal()
    try:
        result = run(
            session,
            product_ids=ids,
            package_quantity=args.package_quantity,
            package_unit=args.package_unit,
            commit=args.commit,
        )
    finally:
        session.close()
    _print_summary(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
