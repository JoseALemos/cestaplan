"""set_product_packaging: corrects package_quantity/unit on a product's latest price."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from cestaplan_api.models import Product, ProductPrice
from cestaplan_api.tools.set_product_packaging import run
from tests.fixtures.provider_scenarios import seed_test_retailer, seed_test_store


def _product_with_price(db: Session, name: str, pkg_qty: str) -> int:
    retailer = seed_test_retailer(db, f"r-{abs(hash(name)) % 100000}", name="R")
    store = seed_test_store(db, retailer)
    product = Product(name=name, is_synthetic=False)
    db.add(product)
    db.flush()
    now = datetime.now(UTC)
    db.add(ProductPrice(
        retailer_id=retailer.id, store_id=store.id, product_id=product.id,
        amount=Decimal("1.80"), currency="EUR",
        package_quantity=Decimal(pkg_qty), package_unit="unit",
        source_type="community_connector", source_name="x",
        observed_at=now, imported_at=now, confidence_score=Decimal("1.0"),
    ))
    db.flush()
    return product.id


def test_corrects_package_quantity(db_session: Session) -> None:
    pid = _product_with_price(db_session, "Huevos grandes L", "1")

    dry = run(db_session, product_ids=[pid], package_quantity=Decimal("12"),
              package_unit=None, commit=False)
    assert dry["changed"] == 1
    price = db_session.execute(
        select(ProductPrice).where(ProductPrice.product_id == pid)
    ).scalar_one()
    assert price.package_quantity == Decimal("1")  # dry-run changed nothing

    run(db_session, product_ids=[pid], package_quantity=Decimal("12"),
        package_unit=None, commit=True)
    db_session.refresh(price)
    assert price.package_quantity == Decimal("12") and price.package_unit == "unit"


def test_missing_product_reported(db_session: Session) -> None:
    result = run(db_session, product_ids=[999_999_999], package_quantity=Decimal("12"),
                 package_unit=None, commit=True)
    assert result["changed"] == 0 and result["not_found"] == [999_999_999]
