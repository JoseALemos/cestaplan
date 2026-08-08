"""Apify Mercadona mapper + provider (spec §5-§7) — grounded only in the observed capture.

The mapper turns the Apify Mercadona actor's dataset items into the normalized
:class:`ExternalCatalogProduct` without inventing anything:

- ``ean`` is ``null`` for every sampled item, so ``barcode`` stays ``None`` when absent and is
  only ever the value the source reports (§7 — never fabricated from the name).
- net content is NOT extracted from the ``unit`` string ("6 x 6 l"); ``net_content_quantity`` /
  ``net_content_unit`` stay ``None``. Instead Mercadona publishes a real reference **unit price**
  ("0.84/L"), so — exactly like DIA — the product is costed by ``unit_price`` (see
  ``quality.UNIT_PRICE_COSTED_PROVIDERS``), never by a guessed package size.
- ``price_scope`` is ``postal_code``: Mercadona prices vary by delivery zone (postal code). The
  provider runs with one postal code (``apify_mercadona_default_postal_code``) and threads it to
  the mapper, which stamps it on every product. Without a postal code the scope is ``UNKNOWN``
  (the honest limit — a price with no zone cannot be localised).
- ``observed_at`` is the source's own ``scrapedAt`` (ISO-8601, tz-aware) — a genuine observation
  time, not merely the retrieval time.
- a promotion is only applied when ``promotionPrice`` is present AND genuinely below the regular
  price; an ambiguous/absent promo is never read as a markdown.
- an unknown schema fingerprint blocks normalization.

Schema pinning: the fingerprint is computed over the REQUIRED core — the always-present,
stable-typed fields the mapper depends on. ``ean`` and ``promotionPrice`` are ``null`` in the
capture and legitimately nullable, so they are deliberately EXCLUDED from the core; that keeps the
fingerprint stable when real data populates them (a string EAN / a float promo price still matches
the pinned core), while any structural drift in a depended-on field still blocks.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Annotated

from pydantic import BaseModel, BeforeValidator, ConfigDict

from cestaplan_api.config import Settings, get_settings
from cestaplan_api.ingestion.contracts import PriceScope, PriceType
from cestaplan_api.ingestion.providers.apify.client import ApifyClient
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
from cestaplan_api.ingestion.providers.schema_tools import merge_samples, schema_fingerprint

# ``unitPrice`` unit tokens Mercadona uses (right of the "/") -> our unit codes. Unknown -> the
# unit price is dropped (never guessed), so the product simply isn't costable by unit price.
_UNIT_PRICE_UNITS = {
    "l": "l",
    "litro": "l",
    "kg": "kg",
    "kilo": "kg",
    "kilogramo": "kg",
    "ml": "ml",
    "g": "g",
    "gramo": "g",
}
# The required-field ("core") projection whose structure the mapper is pinned to. ``ean`` and
# ``promotionPrice`` are excluded on purpose (nullable in the capture — see module docstring).
_REQUIRED = (
    "brand",
    "category",
    "currency",
    "imageUrl",
    "inStock",
    "name",
    "price",
    "scrapedAt",
    "sku",
    "unit",
    "unitPrice",
    "url",
)


def _to_decimal(value: object) -> object:
    # money arrives as a JSON number; go through str to avoid float imprecision.
    return Decimal(str(value)) if value is not None and not isinstance(value, Decimal) else value


Money = Annotated[Decimal, BeforeValidator(_to_decimal)]


class UnsupportedSchemaError(ProviderError):
    """The batch's schema fingerprint is not one the mapper is validated against."""


class ApifyMercadonaRecord(BaseModel):
    """One Apify Mercadona dataset item — derived ONLY from the observed capture.

    Critical fields (present in every sampled item) are required; ``ean`` / ``promotionPrice`` are
    nullable. ``extra="ignore"`` tolerates new fields without failing.
    """

    model_config = ConfigDict(extra="ignore")

    name: str
    brand: str
    sku: str
    price: Money
    currency: str
    unit: str
    unitPrice: str
    category: str
    url: str
    imageUrl: str
    inStock: bool
    scrapedAt: str
    ean: str | None = None
    promotionPrice: Money | None = None


def _parse_observed_at(raw: str) -> datetime:
    """Parse the source ``scrapedAt`` ISO-8601 timestamp as a tz-aware datetime (UTC if naive)."""
    normalized = raw.strip()
    if normalized.endswith("Z"):  # 3.11+ handles 'Z', but normalise for safety across versions
        normalized = normalized[:-1] + "+00:00"
    parsed = datetime.fromisoformat(normalized)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _parse_unit_price(raw: str) -> tuple[Decimal | None, str | None]:
    """Parse a Mercadona unit price like ``"0.84/L"`` -> ``(Decimal("0.84"), "l")``.

    Returns ``(None, None)`` when the string is not a clean ``value/unit`` with a positive value
    and a known unit — the mapper never guesses.
    """
    value_part, sep, unit_part = raw.partition("/")
    if not sep:
        return None, None
    try:
        value = Decimal(value_part.strip().replace(",", "."))
    except (InvalidOperation, ValueError):
        return None, None
    if value <= 0:
        return None, None
    unit = _UNIT_PRICE_UNITS.get(unit_part.strip().lower())
    if unit is None:
        return None, None
    return value, unit


class ApifyMercadonaMapper:
    mapping_version = "1.0.0"
    retailer_slug = "mercadona"
    provider_code = "apify-mercadona"
    # Fingerprint of the required-field core observed in the sanitized Mercadona sample.
    supported_schema_fingerprints = (
        "9ceac16cdf5b4eecef265f64757c4f7790263f177fb9158cd75b53a35b412002",
    )

    def detect_schema(self, records: list[dict]) -> str:
        """Fingerprint of the required-field core (stable to nullable-field variance)."""
        core = [{k: r[k] for k in _REQUIRED if k in r} for r in records]
        return schema_fingerprint(merge_samples(core))

    def validate_supported_schema(self, records: list[dict]) -> str:
        fp = self.detect_schema(records)
        if fp not in self.supported_schema_fingerprints:
            raise UnsupportedSchemaError(
                f"unknown Mercadona schema fingerprint {fp}; capture + review before mapping"
            )
        return fp

    def map_products(
        self, records: list[dict], *, postal_code: str | None = None
    ) -> list[ExternalCatalogProduct]:
        if not records:  # empty response -> nothing to normalize (not an error)
            return []
        self.validate_supported_schema(records)  # unknown fingerprint blocks normalization
        return [
            self.map_product(ApifyMercadonaRecord.model_validate(r), postal_code=postal_code)
            for r in records
        ]

    def map_product(
        self, record: ApifyMercadonaRecord, *, postal_code: str | None = None
    ) -> ExternalCatalogProduct:
        regular = record.price
        promotional = self._promotional_price(regular, record.promotionPrice)
        unit_price, unit_price_unit = _parse_unit_price(record.unitPrice)
        observed_at = _parse_observed_at(record.scrapedAt)
        return ExternalCatalogProduct(
            provider=self.provider_code,
            retailer_slug=self.retailer_slug,
            external_product_id=record.sku,
            product_name=record.name,
            brand=record.brand or None,
            category=record.category or None,
            barcode=record.ean or None,  # null/empty -> None; never invented
            sell_unit=SellUnit.PACKAGE,  # sold as a package; net content not extracted
            regular_price=regular,
            promotional_price=promotional,
            currency=record.currency,  # taken from the response, not assumed
            price_scope=self.map_scope(postal_code),  # postal_code (zone) — see map_scope
            postal_code=postal_code or None,
            observed_at=observed_at,  # source's own scrapedAt (tz-aware)
            availability=Availability.IN_STOCK if record.inStock else Availability.OUT_OF_STOCK,
            variable_weight=False,
            net_content_quantity=None,  # §7: not extracted from the "unit" string
            net_content_unit=None,
            unit_price=unit_price,
            unit_price_unit=unit_price_unit,
            image_url=record.imageUrl or None,
            product_url=record.url or None,
            promotion=self.map_promotion(regular, record.promotionPrice),
            verification_status=ProviderVerificationStatus.PROVIDER_REPORTED,
            confidence_score=Decimal("1.0"),
            raw_source_reference=(
                f"sku:{record.sku}; source_observed_at={observed_at.isoformat()}; "
                f"postal_code={postal_code or 'none'}; mapping={self.mapping_version}"
            ),
        )

    def _promotional_price(self, regular: Decimal, promo: Decimal | None) -> Decimal | None:
        """A promo price only when present AND genuinely below the regular price."""
        if promo is not None and 0 < promo < regular:
            return promo
        return None

    def map_promotion(self, regular: Decimal, promo: Decimal | None) -> ProviderPromotion | None:
        promotional = self._promotional_price(regular, promo)
        if promotional is None:
            return None
        percentage = ((regular - promotional) / regular * Decimal("100")).quantize(Decimal("0.01"))
        return ProviderPromotion(
            price_type=PriceType.PROMOTIONAL,
            promotional_price=promotional,
            percentage_discount=percentage,
        )

    def map_scope(self, postal_code: str | None) -> PriceScope:
        # Mercadona prices are per delivery zone (postal code). With a postal code the scope IS
        # determinable at postal-code granularity; without one a price cannot be localised.
        return PriceScope.POSTAL_CODE if postal_code else PriceScope.UNKNOWN


class ApifyMercadonaProvider(PriceCatalogProvider):
    provider_code = "apify-mercadona"

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        client: ApifyClient | None = None,
        mapper: ApifyMercadonaMapper | None = None,
    ) -> None:
        s = settings or get_settings()
        self._settings = s
        self._mapper = mapper or ApifyMercadonaMapper()
        self._postal_code = s.apify_mercadona_default_postal_code or ""
        if client is not None:
            self._client: ApifyClient | None = client
        elif s.apify_enabled and s.apify_mercadona_enabled and s.apify_api_token:
            self._client = ApifyClient(
                api_token=s.apify_api_token,
                base_url=s.apify_base_url,
                max_wait_seconds=s.apify_max_wait_seconds,
                poll_interval_seconds=s.apify_poll_interval_seconds,
            )
        else:
            self._client = None

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            full_catalog=False,  # bounded actor run; not a full-catalogue guarantee
            store_scope=True,  # postal-code (delivery-zone) scoped prices
            incremental_sync=False,
            promotions=True,
            categories=True,
            search=False,  # actor input is a bounded maxItems run, not a search query
        )

    def get_source_metadata(self) -> ProviderMetadata:
        return ProviderMetadata(
            provider_code=self.provider_code,
            retailer_slug="mercadona",
            kind=ProviderKind.INDEPENDENT,  # third-party Apify actor, NOT Mercadona's official API
            status=ProviderStatus.ACTIVE_WHEN_CONFIGURED,
            official=False,
            catalog_type="search_partial",
            attribution="Apify (actor de terceros). No es una API oficial de Mercadona.",
        )

    def health_check(self) -> HealthStatus:
        # No cheap Apify ping exists and this layer performs no speculative network call; report
        # the honest configured/not-configured state instead.
        if self._client is None:
            return HealthStatus(ok=False, detail="apify mercadona not configured")
        return HealthStatus(
            ok=True, detail="apify mercadona configured", checked_at=datetime.now(UTC)
        )

    def iterate_products(self, query: ProductQuery) -> Iterator[ExternalCatalogProduct]:
        if self._client is None:
            raise NotSupportedError("apify mercadona not configured (missing token/flags)")
        limit = query.max_products or 30
        postal_code = query.postal_code or self._postal_code
        # Actor input: the capturer used ``{"maxItems": limit}``; the postal code is threaded here
        # under ``postalCode`` (assumed key for the zone). An empty postal code is omitted.
        run_input: dict[str, object] = {"maxItems": limit}
        if postal_code:
            run_input["postalCode"] = postal_code
        run_id = self._client.start_run(self._settings.apify_mercadona_actor_id, run_input)
        run = self._client.wait_for_run(run_id)
        records = self._client.get_dataset_items(str(run["defaultDatasetId"]), limit=limit)[:limit]
        yield from self._mapper.map_products(records, postal_code=postal_code or None)


__all__ = [
    "ApifyMercadonaMapper",
    "ApifyMercadonaProvider",
    "ApifyMercadonaRecord",
    "UnsupportedSchemaError",
]
