"""DemoCatalogProvider (FASE 1).

Emits a small, deterministic set of :class:`ExternalCatalogProduct` from synthetic fixtures
so the whole provider contract can be exercised end to end without any network or credential.
It is never presented as an official source (``kind=demo``, ``official=False``).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal

from cestaplan_api.ingestion.contracts import PriceScope
from cestaplan_api.ingestion.providers.contracts import (
    Availability,
    ContentUnit,
    ExternalCatalogProduct,
    HealthStatus,
    PriceCatalogProvider,
    ProductQuery,
    ProviderCapabilities,
    ProviderKind,
    ProviderMetadata,
    ProviderStatus,
    SellUnit,
)

# (external_id, name, price, net_qty, net_unit)
_FIXTURES: tuple[tuple[str, str, str, str, ContentUnit], ...] = (
    ("DEMO-LECHE-1L", "Leche desnatada 1 L (demo)", "0.88", "1000", ContentUnit.ML),
    ("DEMO-GARB-400", "Garbanzos cocidos 400 g (demo)", "0.91", "400", ContentUnit.G),
    ("DEMO-VIN-750", "Vinagre de vino 750 ml (demo)", "0.87", "750", ContentUnit.ML),
)


class DemoCatalogProvider(PriceCatalogProvider):
    provider_code = "demo"
    retailer_slug = "mercaejemplo"

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            full_catalog=True,
            store_scope=False,
            incremental_sync=False,
            promotions=False,
            categories=False,
            search=False,
        )

    def get_source_metadata(self) -> ProviderMetadata:
        return ProviderMetadata(
            provider_code=self.provider_code,
            retailer_slug=self.retailer_slug,
            kind=ProviderKind.DEMO,
            status=ProviderStatus.COMPLEMENTARY,
            official=False,
            catalog_type="synthetic",
            attribution="Datos sintéticos de demostración; no representan a ninguna cadena.",
        )

    def health_check(self) -> HealthStatus:
        return HealthStatus(
            ok=True, detail="demo provider always healthy", checked_at=datetime.now(UTC)
        )

    def iterate_products(self, query: ProductQuery) -> Iterator[ExternalCatalogProduct]:
        now = datetime.now(UTC)
        limit = query.max_products
        for index, (external_id, name, price, qty, unit) in enumerate(_FIXTURES):
            if limit is not None and index >= limit:
                return
            yield ExternalCatalogProduct(
                provider=self.provider_code,
                retailer_slug=self.retailer_slug,
                external_product_id=external_id,
                product_name=name,
                sell_unit=SellUnit.PACKAGE,
                regular_price=Decimal(price),
                currency="EUR",
                price_scope=PriceScope.NATIONAL,
                observed_at=now,
                availability=Availability.IN_STOCK,
                net_content_quantity=Decimal(qty),
                net_content_unit=unit,
                package_quantity=Decimal(qty),
                package_unit=unit,
            )


__all__ = ["DemoCatalogProvider"]
