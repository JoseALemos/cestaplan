"""GAP 1: the main sync path must persist the reference unit price on the ProductVariant, so a
unit-price-costed provider (DIA) is costable from stored data. Without it, classify_variant_costing_
mode would see unit_price=None and grade the variant UNRESOLVED. Fully synthetic, no network."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from cestaplan_api.ingestion.contracts import PriceScope
from cestaplan_api.ingestion.providers.contracts import (
    Availability,
    ExternalCatalogProduct,
    ProductCostingMode,
    SellUnit,
)
from cestaplan_api.ingestion.providers.onboarding import classify_variant_costing_mode
from cestaplan_api.models import Retailer
from cestaplan_api.services.provider_sync import _upsert_variant

_NOW = datetime(2026, 7, 23, 12, 0, tzinfo=UTC)


def _dia_product() -> ExternalCatalogProduct:
    """A DIA-shaped product: national price, real €/l unit price, NO net content."""
    return ExternalCatalogProduct(
        provider="parsebot-dia",
        retailer_slug="dia",
        external_product_id="DIA-1",
        product_name="Leche entera DIA",
        sell_unit=SellUnit.PACKAGE,
        regular_price=Decimal("0.84"),
        currency="EUR",
        price_scope=PriceScope.NATIONAL,
        observed_at=_NOW,
        availability=Availability.IN_STOCK,
        variable_weight=False,
        net_content_quantity=None,
        net_content_unit=None,
        unit_price=Decimal("0.84"),
        unit_price_unit="l",
    )


def test_upsert_variant_persists_unit_price(db_session: Session) -> None:
    retailer = Retailer(slug="dia-gap1", name="DIA gap1", adapter_key="parsebot-dia")
    db_session.add(retailer)
    db_session.flush()

    variant = _upsert_variant(db_session, retailer.id, _dia_product())

    # GAP 1: the reference unit price is now stored on the variant (previously always None).
    assert variant.unit_price == Decimal("0.84")
    assert variant.unit_price_unit == "l"
    # ...which makes the DIA variant costable via the provider-scoped exception.
    mode = classify_variant_costing_mode(
        sell_unit=variant.sell_unit,
        variable_weight=variant.variable_weight,
        net_content_quantity=variant.net_content_quantity,
        net_content_unit=variant.net_content_unit,
        unit_price=variant.unit_price,
        unit_price_unit=variant.unit_price_unit,
        has_price=True,
        provider_code="parsebot-dia",
    )
    assert mode is ProductCostingMode.VARIABLE_VOLUME


def test_upsert_variant_refreshes_unit_price_on_resync(db_session: Session) -> None:
    retailer = Retailer(slug="dia-gap1b", name="DIA gap1b", adapter_key="parsebot-dia")
    db_session.add(retailer)
    db_session.flush()

    _upsert_variant(db_session, retailer.id, _dia_product())
    updated = _dia_product()
    updated.unit_price = Decimal("0.90")  # price moved on the next sync
    variant = _upsert_variant(db_session, retailer.id, updated)

    assert variant.unit_price == Decimal("0.90")  # kept fresh, not stuck at the first value
