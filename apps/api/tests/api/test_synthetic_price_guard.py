"""Guard de integridad: los precios sintéticos (demo) nunca costean un plan de cadena real.

``_latest_price_by_product`` filtra por ``retailer_id`` (las cadenas nunca se mezclan). El
catálogo demo vive bajo su propio retailer sintético, así que:

* seleccionar una cadena real -> sólo sus precios (ningún sintético);
* seleccionar la cadena demo -> sus precios sintéticos (la demo sigue costeando);
* sin cadena (``retailer_id is None``) -> se excluyen explícitamente los sintéticos, para que
  nunca se filtren a un plan que no eligió la demo.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from cestaplan_api.models import Product, ProductPrice, Retailer, Store
from cestaplan_api.services.plan_service import _latest_price_by_product


def _make_real_price(db: Session) -> tuple[Retailer, Product]:
    """Create a real (non-synthetic) retailer -> store -> product -> price chain."""
    now = datetime.now(UTC)
    suffix = uuid.uuid4().hex[:8]
    retailer = Retailer(
        slug=f"real-chain-{suffix}",
        name="Cadena Real de Prueba",
        adapter_key="test",
        is_synthetic=False,
    )
    db.add(retailer)
    db.flush()
    store = Store(retailer_id=retailer.id, name="Tienda Real", is_synthetic=False)
    db.add(store)
    db.flush()
    product = Product(
        retailer_id=retailer.id,
        external_id=f"REAL-{suffix}",
        name="Producto Real",
        package_quantity=Decimal("1"),
        package_unit="kg",
        is_synthetic=False,
    )
    db.add(product)
    db.flush()
    db.add(
        ProductPrice(
            retailer_id=retailer.id,
            store_id=store.id,
            product_id=product.id,
            amount=Decimal("2.50"),
            currency="EUR",
            package_quantity=Decimal("1"),
            package_unit="kg",
            unit_price=Decimal("2.50"),
            source_type="manual_entry",
            source_name="prueba",
            observed_at=now,
            imported_at=now,
            confidence_score=Decimal("1.0"),
            is_synthetic=False,
        )
    )
    db.flush()
    return retailer, product


def _demo_retailer(db: Session) -> Retailer:
    demo = (
        db.execute(select(Retailer).where(Retailer.is_synthetic.is_(True)))
        .scalars()
        .first()
    )
    assert demo is not None, "El catálogo demo (retailer sintético) debe estar sembrado"
    return demo


def test_no_chain_selected_excludes_synthetic_prices(db_session: Session) -> None:
    # Un plan sin cadena seleccionada NUNCA debe recibir precios sintéticos, aunque la demo
    # esté sembrada; y sí debe ver los precios reales.
    _retailer, real_product = _make_real_price(db_session)
    latest = _latest_price_by_product(db_session, None)
    assert latest, "debería devolver al menos el precio real"
    assert all(not price.is_synthetic for price in latest.values())
    assert real_product.id in latest


def test_selecting_demo_retailer_still_costs_with_synthetic(db_session: Session) -> None:
    # La demo NO se borra: si se selecciona su cadena, sus precios sintéticos sí costean.
    demo = _demo_retailer(db_session)
    latest = _latest_price_by_product(db_session, demo.id)
    assert latest, "la cadena demo debe tener precios"
    assert all(price.retailer_id == demo.id for price in latest.values())
    assert all(price.is_synthetic for price in latest.values())


def test_real_chain_never_gets_synthetic_prices(db_session: Session) -> None:
    real_retailer, real_product = _make_real_price(db_session)
    latest = _latest_price_by_product(db_session, real_retailer.id)
    assert real_product.id in latest
    assert all(not price.is_synthetic for price in latest.values())
    assert all(price.retailer_id == real_retailer.id for price in latest.values())
