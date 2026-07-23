"""Price-provider layer FASE 1: contract, registry, demo provider, usage model.

No network. Verifies the registry wiring, the demo provider's contract output (Decimal
money, net content, never 'official'), the max_products bound, and that the ProviderUsage
accounting row persists.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from cestaplan_api.ingestion.contracts import PriceScope
from cestaplan_api.ingestion.providers.contracts import (
    Availability,
    ContentUnit,
    ProductQuery,
    ProviderKind,
    SellUnit,
)
from cestaplan_api.ingestion.providers.exceptions import NotSupportedError
from cestaplan_api.ingestion.providers.registry import registry
from cestaplan_api.models import CrawlRun, ProviderUsage, Retailer


def test_registry_has_demo_and_rejects_unknown() -> None:
    assert "demo" in registry.codes()
    provider = registry.get("demo")
    assert provider.provider_code == "demo"
    with pytest.raises(NotSupportedError):
        registry.get("does-not-exist")


def test_demo_provider_metadata_never_official() -> None:
    meta = registry.get("demo").get_source_metadata()
    assert meta.official is False
    assert meta.kind is ProviderKind.DEMO


def test_demo_provider_products_use_decimal_and_net_content() -> None:
    products = list(registry.get("demo").iterate_products(ProductQuery()))
    assert len(products) == 3
    first = products[0]
    assert isinstance(first.regular_price, Decimal)  # money is Decimal, never float
    assert first.currency == "EUR"
    assert first.sell_unit is SellUnit.PACKAGE
    assert first.price_scope is PriceScope.NATIONAL
    assert first.availability is Availability.IN_STOCK
    assert first.net_content_unit in {ContentUnit.ML, ContentUnit.G}
    assert first.net_content_quantity is not None


def test_demo_provider_respects_max_products() -> None:
    products = list(registry.get("demo").iterate_products(ProductQuery(max_products=1)))
    assert len(products) == 1


def test_demo_health_check_ok() -> None:
    assert registry.get("demo").health_check().ok is True


def test_provider_usage_row_persists(db_session: Session) -> None:
    retailer = Retailer(slug="pu-test", name="PU", adapter_key="x", is_synthetic=True)
    db_session.add(retailer)
    db_session.flush()
    run = CrawlRun(retailer_id=retailer.id, run_type="prices", status="completed")
    db_session.add(run)
    db_session.flush()

    db_session.add(
        ProviderUsage(
            provider="demo",
            operation="iterate_products",
            request_count=1,
            product_count=3,
            estimated_cost=Decimal("0.0100"),
            currency="EUR",
            started_at=datetime(2026, 7, 23, 9, 0, tzinfo=UTC),
            completed_at=datetime(2026, 7, 23, 9, 1, tzinfo=UTC),
            crawl_run_id=run.id,
        )
    )
    db_session.flush()

    row = db_session.execute(
        select(ProviderUsage).where(ProviderUsage.provider == "demo")
    ).scalars().one()
    assert row.product_count == 3
    assert row.estimated_cost == Decimal("0.0100")
    assert row.crawl_run_id == run.id
