"""C2: el carril de planificación no costea con un precio de OTRA zona.

_latest_prices restringe a las tiendas del CP configurado (+ nacionales); un precio de otra zona,
aunque sea más nuevo/barato, no se usa.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from cestaplan_api.models import Product, ProductPrice, Retailer, Store
from cestaplan_api.services import planning_context as pc

_NOW = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)


class _Settings:
    def __init__(self, postal: str) -> None:
        self.mercadona_postal_code = postal


def _price(
    db: Session, retailer_id: int, store_id: int | None, product_id: int, amount: str,
    observed_at: datetime,
) -> None:
    db.add(ProductPrice(
        retailer_id=retailer_id, store_id=store_id, product_id=product_id,
        amount=Decimal(amount), currency="EUR", package_quantity=Decimal("1"), package_unit="unit",
        source_type="manual_entry", source_name="t",
        observed_at=observed_at, imported_at=observed_at,
        confidence_score=Decimal("1.0"), is_synthetic=False,
    ))


def _setup(db: Session) -> tuple[int, int]:
    r = Retailer(slug="zone-test", name="ZoneTest", adapter_key="test", is_synthetic=False)
    db.add(r)
    db.flush()
    here = Store(retailer_id=r.id, name="Aqui 14006", postal_code="14006", is_synthetic=False)
    other = Store(retailer_id=r.id, name="Otra 28001", postal_code="28001", is_synthetic=False)
    db.add_all([here, other])
    db.flush()
    prod = Product(name="Producto zona", package_quantity=Decimal("1"), package_unit="unit",
                   is_synthetic=False)
    db.add(prod)
    db.flush()
    # Zona correcta (14006): más antigua y más cara. Zona equivocada (28001): más nueva y barata.
    _price(db, r.id, here.id, prod.id, "2.00", _NOW - timedelta(hours=1))
    _price(db, r.id, other.id, prod.id, "1.00", _NOW)
    db.flush()
    return r.id, prod.id


def test_latest_prices_excludes_other_zone(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    retailer_id, product_id = _setup(db_session)
    monkeypatch.setattr(pc, "get_settings", lambda: _Settings("14006"))
    latest = pc._latest_prices(db_session, retailer_id)
    assert product_id in latest
    # Toma el precio de la zona 14006 (2,00), NO el más nuevo/barato de la zona 28001 (1,00).
    assert latest[product_id].amount == Decimal("2.00")


def test_zone_store_ids_empty_without_config(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    retailer_id, _ = _setup(db_session)
    monkeypatch.setattr(pc, "get_settings", lambda: _Settings(""))
    assert pc.zone_store_ids(db_session, retailer_id) == []


def test_no_zone_config_uses_newest_across_stores(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Sin CP configurado: comportamiento previo (más nuevo por producto, cualquier tienda).
    retailer_id, product_id = _setup(db_session)
    monkeypatch.setattr(pc, "get_settings", lambda: _Settings(""))
    latest = pc._latest_prices(db_session, retailer_id)
    assert latest[product_id].amount == Decimal("1.00")
