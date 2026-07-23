"""Parse.bot DIA mapper + provider (spec §5-§7) — grounded only in the observed schema.

The mapper turns DIA ``search_items`` into the normalized :class:`ExternalCatalogProduct`
without inventing anything:

- barcode / package_quantity / package_unit / net_content are NOT in DIA's search response,
  so they stay ``None`` (never extracted from the name — §7). The product is therefore not
  costable for recipes; that is the honest limit of this endpoint.
- price_scope is ``unknown`` (no store/zone/postal in the response — §6), never exact_store.
- observed_at is the retrieval time (``retrieved_at``); the source provides no timestamp, so
  ``source_observed_at`` is recorded as absent in ``raw_source_reference`` (§5).
- a promotion is only applied when flagged AND the strikethrough price is genuinely higher;
  an ambiguous promo is never read as a regular price.
- an unknown schema fingerprint blocks normalization.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal

from cestaplan_api.config import Settings, get_settings
from cestaplan_api.ingestion.contracts import PriceScope, PriceType
from cestaplan_api.ingestion.providers.contracts import (
    Availability,
    ExternalCatalogProduct,
    HealthStatus,
    PriceCatalogProvider,
    ProductQuery,
    ProviderCapabilities,
    ProviderKind,
    ProviderMetadata,
    ProviderPromotion,
    ProviderStatus,
    ProviderVerificationStatus,
    SellUnit,
)
from cestaplan_api.ingestion.providers.exceptions import NotSupportedError, ProviderError
from cestaplan_api.ingestion.providers.parsebot.client import ParseBotClient
from cestaplan_api.ingestion.providers.parsebot.schemas import ParseBotDiaPrices, ParseBotDiaProduct
from cestaplan_api.ingestion.providers.schema_tools import merge_samples, schema_fingerprint

# Spanish measure-unit words DIA uses -> our unit codes. Unknown -> None (never guessed).
_UNIT_ALIASES = {
    "litro": "l",
    "litros": "l",
    "l": "l",
    "kilo": "kg",
    "kilos": "kg",
    "kilogramo": "kg",
    "kg": "kg",
    "gramo": "g",
    "gramos": "g",
    "g": "g",
    "mililitro": "ml",
    "ml": "ml",
    "unidad": "unit",
    "unidades": "unit",
    "ud": "unit",
}
# The required-field ("core") projection whose structure the mapper is pinned to.
_REQUIRED = (
    "sku_id",
    "display_name",
    "brand",
    "brand_type",
    "l1_category_description",
    "l2_category_description",
    "image",
    "url",
    "object_id",
    "units_in_stock",
    "units_in_cart",
    "prices",
)


class UnsupportedSchemaError(ProviderError):
    """The batch's schema fingerprint is not one the mapper is validated against."""


class ParseBotDiaMapper:
    mapping_version = "1.0.0"
    retailer_slug = "dia"
    provider_code = "parsebot-dia"
    # Fingerprint of the required-field core observed in the sanitized DIA sample.
    supported_schema_fingerprints = (
        "c9fe8f4ae1564029df9c0f66fc01753029170342a19483d0cec22f2daa3fcbfa",
    )

    def detect_schema(self, records: list[dict]) -> str:
        """Fingerprint of the required-field core (stable to optional-field variance)."""
        core = [{k: r[k] for k in _REQUIRED if k in r} for r in records]
        return schema_fingerprint(merge_samples(core))

    def validate_supported_schema(self, records: list[dict]) -> str:
        fp = self.detect_schema(records)
        if fp not in self.supported_schema_fingerprints:
            raise UnsupportedSchemaError(
                f"unknown DIA schema fingerprint {fp}; capture + review before mapping"
            )
        return fp

    def map_products(
        self, records: list[dict], *, retrieved_at: datetime
    ) -> list[ExternalCatalogProduct]:
        if not records:  # empty response -> nothing to normalize (not an error)
            return []
        self.validate_supported_schema(records)  # unknown fingerprint blocks normalization
        return [
            self.map_product(ParseBotDiaProduct.model_validate(r), retrieved_at) for r in records
        ]

    def map_product(
        self, product: ParseBotDiaProduct, retrieved_at: datetime
    ) -> ExternalCatalogProduct:
        regular, promotional, loyalty = self.map_price(product.prices)
        unit_price, unit_price_unit = self._unit_price(product.prices)
        return ExternalCatalogProduct(
            provider=self.provider_code,
            retailer_slug=self.retailer_slug,
            external_product_id=product.sku_id,
            product_name=product.display_name,
            brand=product.brand or None,
            category=product.l2_category_description or None,
            barcode=None,  # not provided by DIA search — never invented
            sell_unit=SellUnit.PACKAGE,  # sold as a package; net content unknown
            regular_price=regular,
            promotional_price=promotional,
            loyalty_price=loyalty,
            currency=product.prices.currency,  # taken from the response, not assumed
            price_scope=self.map_scope(),  # unknown — no store/zone evidence
            observed_at=retrieved_at,  # retrieval time; source provides none (§5)
            availability=self.map_availability(product),
            variable_weight=False,
            net_content_quantity=None,  # §7: absent, not extracted from the name
            net_content_unit=None,
            unit_price=unit_price,
            unit_price_unit=unit_price_unit,
            image_url=product.image or None,
            product_url=product.url or None,
            promotion=self.map_promotion(product.prices),
            verification_status=ProviderVerificationStatus.PROVIDER_REPORTED,
            confidence_score=Decimal("1.0"),
            raw_source_reference=(
                f"sku:{product.sku_id}; source_observed_at=absent; "
                f"retrieved_at={retrieved_at.isoformat()}; mapping={self.mapping_version}"
            ),
        )

    def map_price(
        self, prices: ParseBotDiaPrices
    ) -> tuple[Decimal, Decimal | None, Decimal | None]:
        """Return ``(regular, promotional, loyalty)``. Never reads an ambiguous promo as regular."""
        loyalty = prices.price if prices.is_club_price else None
        if prices.is_promo_price and prices.strikethrough_price > prices.price > 0:
            return prices.strikethrough_price, prices.price, loyalty
        # not a clear markdown -> the current price is the regular price
        return prices.price, None, loyalty

    def map_promotion(self, prices: ParseBotDiaPrices) -> ProviderPromotion | None:
        if not prices.is_promo_price or not (prices.strikethrough_price > prices.price > 0):
            return None
        return ProviderPromotion(
            price_type=PriceType.PROMOTIONAL,
            promotional_price=prices.price,
            percentage_discount=Decimal(prices.discount_percentage),
        )

    def map_availability(self, product: ParseBotDiaProduct) -> Availability:
        return Availability.IN_STOCK if product.units_in_stock > 0 else Availability.OUT_OF_STOCK

    def map_package(self) -> tuple[None, None]:
        return None, None  # DIA search exposes no package size (§7)

    def map_scope(self) -> PriceScope:
        return PriceScope.UNKNOWN  # §6: no store/postal/zone in the response

    def _unit_price(self, prices: ParseBotDiaPrices) -> tuple[Decimal | None, str | None]:
        unit = _UNIT_ALIASES.get(prices.measure_unit.strip().lower())
        if unit is not None and prices.price_per_unit > 0:
            return prices.price_per_unit, unit
        return None, None


class ParseBotDiaProvider(PriceCatalogProvider):
    provider_code = "parsebot-dia"

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        client: ParseBotClient | None = None,
        mapper: ParseBotDiaMapper | None = None,
        query: str = "leche",
    ) -> None:
        s = settings or get_settings()
        self._mapper = mapper or ParseBotDiaMapper()
        self._query = query
        if client is not None:
            self._client: ParseBotClient | None = client
        elif s.parse_bot_api_key and s.parse_bot_dia_base_url:
            self._client = ParseBotClient(
                base_url=s.parse_bot_dia_base_url,
                api_key=s.parse_bot_api_key,
                timeout=s.parse_bot_timeout_seconds,
                max_retries=s.parse_bot_max_retries,
            )
        else:
            self._client = None

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            full_catalog=False,  # search-based; not a full catalogue
            store_scope=False,  # response carries no store/zone
            incremental_sync=False,
            promotions=True,
            categories=True,
            search=True,
        )

    def get_source_metadata(self) -> ProviderMetadata:
        return ProviderMetadata(
            provider_code=self.provider_code,
            retailer_slug="dia",
            kind=ProviderKind.INDEPENDENT,  # third-party scraper API, NOT DIA's official API
            status=ProviderStatus.ACTIVE_WHEN_CONFIGURED,
            official=False,
            catalog_type="search_partial",
            attribution="Parse.bot (scraper de terceros). No es una API oficial de DIA.",
        )

    def health_check(self) -> HealthStatus:
        if self._client is None:
            return HealthStatus(ok=False, detail="parse.bot DIA not configured")
        try:
            self._client.get_json("/get_categories", {})
        except Exception as exc:
            return HealthStatus(ok=False, detail=f"unreachable: {type(exc).__name__}")
        return HealthStatus(ok=True, detail="parse.bot DIA reachable", checked_at=datetime.now(UTC))

    def iterate_products(self, query: ProductQuery) -> Iterator[ExternalCatalogProduct]:
        if self._client is None:
            raise NotSupportedError("parse.bot DIA not configured (missing key/base URL)")
        limit = query.max_products or 30
        data = self._client.get_json(
            "/search_products", {"query": query.search or self._query, "limit": limit}
        )
        inner = data.get("data", data) if isinstance(data, dict) else data
        items = inner.get("search_items", []) if isinstance(inner, dict) else inner
        records = list(items)[:limit] if isinstance(items, list) else []
        retrieved_at = datetime.now(UTC)
        yield from self._mapper.map_products(records, retrieved_at=retrieved_at)


__all__ = ["ParseBotDiaMapper", "ParseBotDiaProvider", "UnsupportedSchemaError"]
