"""OpenPricesProvider (FASE 5 piece, no credentials).

Wraps the existing :class:`~cestaplan_api.adapters.openprices.OpenPricesAdapter` (public
ODbL API, no auth, already paginates + parses to :class:`OpenPrice`) and maps each real
observation to the provider contract. Community source, **never official**; used only for
price observations / cross-validation, not as a full daily catalogue (spec §1).

Each Open Prices price is tied to one OpenStreetMap location, so its scope is ``exact_store``.
Rows without a barcode are skipped (a product needs an external id) — never invented; missing
fields stay missing; money stays :class:`~decimal.Decimal`.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal

from cestaplan_api.adapters.openprices import OpenPrice, OpenPricesAdapter
from cestaplan_api.ingestion.contracts import PriceScope
from cestaplan_api.ingestion.providers.contracts import (
    Availability,
    ExternalCatalogProduct,
    HealthStatus,
    PriceCatalogProvider,
    ProductQuery,
    ProviderCapabilities,
    ProviderKind,
    ProviderMetadata,
    ProviderStatus,
    ProviderVerificationStatus,
    SellUnit,
)


def _parse_osm_ref(external_store_id: str) -> tuple[int, str] | None:
    """Parse a store external code ``osm:{TYPE}/{id}`` into ``(osm_id, osm_type)``."""
    ref = external_store_id.strip()
    if ref.lower().startswith("osm:"):
        ref = ref[4:]
    if "/" not in ref:
        return None
    osm_type, _, osm_id = ref.partition("/")
    try:
        return int(osm_id), osm_type.upper()
    except ValueError:
        return None


class OpenPricesProvider(PriceCatalogProvider):
    provider_code = "open-prices"

    def __init__(
        self, adapter: OpenPricesAdapter | None = None, *, retailer_slug: str = "open_prices"
    ) -> None:
        self._adapter = adapter or OpenPricesAdapter()
        self._retailer_slug = retailer_slug

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            full_catalog=False,  # complementary, per-location — never a full daily catalogue
            store_scope=True,
            incremental_sync=True,
            promotions=False,  # discounts surfaced as a lower observed price, never fabricated
            categories=False,
            search=False,
        )

    def get_source_metadata(self) -> ProviderMetadata:
        return ProviderMetadata(
            provider_code=self.provider_code,
            retailer_slug=self._retailer_slug,
            kind=ProviderKind.COMMUNITY,
            status=ProviderStatus.COMPLEMENTARY,
            official=False,
            catalog_type="community_partial",
            attribution="Open Food Facts — Open Prices (ODbL). Community-observed prices.",
        )

    def health_check(self) -> HealthStatus:
        # A cheap ES-locations probe; degrades to a clear message on any transport failure.
        try:
            self._adapter.fetch_locations(country_code="ES")
        except Exception as exc:
            return HealthStatus(ok=False, detail=f"open prices unreachable: {type(exc).__name__}")
        return HealthStatus(ok=True, detail="open prices reachable", checked_at=datetime.now(UTC))

    def iterate_products(self, query: ProductQuery) -> Iterator[ExternalCatalogProduct]:
        """Yield observations for one OSM store (``query.store_external_id``).

        Open Prices is addressable by location; without a store scope there is nothing to
        list, so this yields nothing rather than attempting a full national pull.
        """
        if not query.store_external_id:
            return
        parsed = _parse_osm_ref(query.store_external_id)
        if parsed is None:
            return
        osm_id, osm_type = parsed
        limit = query.max_products
        count = 0
        for price in self._adapter.fetch_store_prices(osm_id, osm_type):
            if price.barcode is None:  # a product needs an external id; never invented
                continue
            if limit is not None and count >= limit:
                return
            count += 1
            yield self._to_product(price, query.store_external_id)

    def _to_product(self, price: OpenPrice, store_external_id: str) -> ExternalCatalogProduct:
        regular = price.amount
        promotional: Decimal | None = None
        if price.price_is_discounted and price.price_without_discount is not None:
            regular = price.price_without_discount
            promotional = price.amount

        by_weight = (price.price_per or "").strip().upper() == "KILOGRAM"
        observed_at = datetime(
            price.observed_on.year, price.observed_on.month, price.observed_on.day, tzinfo=UTC
        )
        return ExternalCatalogProduct(
            provider=self.provider_code,
            retailer_slug=self._retailer_slug,
            external_product_id=price.barcode or str(price.price_id),
            product_name=price.product_name or (price.barcode or str(price.price_id)),
            sell_unit=SellUnit.WEIGHT if by_weight else SellUnit.UNIT,
            regular_price=regular,
            promotional_price=promotional,
            currency=price.currency,
            price_scope=PriceScope.EXACT_STORE,
            observed_at=observed_at,
            availability=Availability.UNKNOWN,
            verification_status=ProviderVerificationStatus.PROVIDER_REPORTED,
            barcode=price.barcode,
            external_store_id=store_external_id,
            unit_price=price.amount if by_weight else None,
            unit_price_unit="kg" if by_weight else None,
            raw_source_reference=price.source_url,
        )


__all__ = ["OpenPricesProvider"]
