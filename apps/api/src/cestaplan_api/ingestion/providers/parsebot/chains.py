"""Parse.bot mappers + providers for Alcampo, Carrefour, Aldi and Lidl — each grounded ONLY in
the observed schema fingerprint of a real bounded capture (see ``.local/provider-samples``).

Shared, pure helpers only (unit parsing, Decimal coercion). Every mapper:
- is pinned to the fingerprint(s) it was validated against; an unknown fingerprint blocks mapping;
- maps only confirmed fields (never invents barcode / package / currency / store / availability);
- uses ``Decimal`` for money and separates regular / promotional / loyalty prices;
- reduces coverage (leaves fields ``None``) rather than guessing when data is absent/ambiguous.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import ClassVar

from cestaplan_api.config import Settings, get_settings
from cestaplan_api.ingestion.contracts import PriceScope, PriceType
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
    ProviderPromotion,
    ProviderStatus,
    ProviderVerificationStatus,
    SellUnit,
)
from cestaplan_api.ingestion.providers.exceptions import NotSupportedError, ProviderError
from cestaplan_api.ingestion.providers.parsebot import plans
from cestaplan_api.ingestion.providers.schema_tools import merge_samples, schema_fingerprint

# ---- shared pure helpers ------------------------------------------------------------------ #

# Only units the contract can represent (G, KG, ML, L, UNIT). Others (cl, mg) are left
# unmapped on purpose -> the size stays uncosted rather than being converted/guessed.
_UNIT_ALIASES: dict[str, ContentUnit] = {
    "l": ContentUnit.L,
    "litro": ContentUnit.L,
    "litros": ContentUnit.L,
    "ml": ContentUnit.ML,
    "mililitro": ContentUnit.ML,
    "mililitros": ContentUnit.ML,
    "kg": ContentUnit.KG,
    "kilo": ContentUnit.KG,
    "kilos": ContentUnit.KG,
    "kilogramo": ContentUnit.KG,
    "g": ContentUnit.G,
    "gr": ContentUnit.G,
    "gramo": ContentUnit.G,
    "gramos": ContentUnit.G,
    "ud": ContentUnit.UNIT,
    "unidad": ContentUnit.UNIT,
    "unidades": ContentUnit.UNIT,
}
# per-litre / per-kilo unit-price unit names some chains use.
_UNIT_PRICE_UNITS: dict[str, str] = {
    "per_litre": "l",
    "per_liter": "l",
    "fop.price.per.litre": "l",
    "per_kilo": "kg",
    "per_kilogram": "kg",
    "fop.price.per.kilo": "kg",
    "kg": "kg",
    "l": "l",
    "ml": "ml",
    "g": "g",
}
_CONTENT_RE = re.compile(r"^\s*([\d]+(?:[.,]\d+)?)\s*(ml|cl|l|mg|kg|g|ud|unidad|unidades)\b", re.I)
_PCT_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*%")


def _dec(value: object) -> Decimal | None:
    """Coerce a number/str to Decimal; never guess. Returns None on empty/invalid."""
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value).replace(",", "."))
    except (InvalidOperation, ValueError):
        return None


def _parse_content(text: object) -> tuple[Decimal | None, ContentUnit | None]:
    """Parse an unambiguous '6000ml' / '1kg' / '500 g' size. Ambiguous -> (None, None)."""
    m = _CONTENT_RE.match(str(text or ""))
    if not m:
        return None, None
    qty = _dec(m.group(1))
    unit = _UNIT_ALIASES.get(m.group(2).lower())
    if qty is None or unit is None:
        return None, None
    return qty, unit


def _pct(text: object) -> Decimal | None:
    m = _PCT_RE.search(str(text or ""))
    return _dec(m.group(1)) if m else None


def _to_dt(value: object, *, fallback: datetime) -> datetime:
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return fallback


class UnsupportedSchemaError(ProviderError):
    """The batch's schema fingerprint is not one the mapper is validated against."""


class _BaseParseBotMapper:
    provider_code: str = ""
    retailer_slug: str = ""
    mapping_version = "1.0.0"
    required_core: tuple[str, ...] = ()
    supported_schema_fingerprints: tuple[str, ...] = ()

    def detect_schema(self, records: list[dict]) -> str:
        core = [{k: r[k] for k in self.required_core if k in r} for r in records]
        return schema_fingerprint(merge_samples(core))

    def validate_supported_schema(self, records: list[dict]) -> str:
        fp = self.detect_schema(records)
        if fp not in self.supported_schema_fingerprints:
            raise UnsupportedSchemaError(
                f"unknown {self.provider_code} schema fingerprint {fp}; capture + review first"
            )
        return fp

    def map_products(
        self, records: list[dict], *, retrieved_at: datetime
    ) -> list[ExternalCatalogProduct]:
        if not records:
            return []
        self.validate_supported_schema(records)
        return [self.map_product(r, retrieved_at) for r in records]

    def map_product(self, r: dict, retrieved_at: datetime) -> ExternalCatalogProduct:
        raise NotImplementedError


# ---- Alcampo (dense catalogue: price + net content + explicit EUR) ------------------------- #


class ParseBotAlcampoMapper(_BaseParseBotMapper):
    provider_code = "parsebot-alcampo"
    retailer_slug = "alcampo"
    required_core = (
        "productId",
        "retailerProductId",
        "name",
        "brand",
        "categoryPath",
        "price",
        "unitPrice",
        "packSizeDescription",
        "available",
        "type",
    )
    supported_schema_fingerprints = (
        "9453bcaef92caffa2238937c22be9e01c928735083ca9399d3d26814c6eb60fa",
    )

    def map_product(self, r: dict, retrieved_at: datetime) -> ExternalCatalogProduct:
        price = r.get("price") or {}
        regular = _dec(price.get("amount"))
        if regular is None:
            raise UnsupportedSchemaError("alcampo record without a decodable price")
        currency = price.get("currency") or "EUR"  # explicit in the response
        qty, unit = _parse_content(r.get("packSizeDescription"))
        up = (r.get("unitPrice") or {}).get("price") or {}
        up_amount = _dec(up.get("amount"))
        up_unit = _UNIT_PRICE_UNITS.get(str((r.get("unitPrice") or {}).get("unitName", "")).lower())
        cats = r.get("categoryPath") or []
        return ExternalCatalogProduct(
            provider=self.provider_code,
            retailer_slug=self.retailer_slug,
            external_product_id=str(r.get("retailerProductId") or r.get("productId")),
            product_name=r.get("name") or "",
            brand=r.get("brand") or None,
            category=(cats[-1] if isinstance(cats, list) and cats else None),
            barcode=None,  # not provided by Alcampo search
            sell_unit=SellUnit.PACKAGE,
            regular_price=regular,
            promotional_price=None,  # no strikethrough/promo price in this endpoint
            loyalty_price=None,
            currency=currency,
            price_scope=PriceScope.UNKNOWN,  # search response carries no store/zone
            observed_at=retrieved_at,
            availability=Availability.IN_STOCK if r.get("available") else Availability.OUT_OF_STOCK,
            variable_weight=False,
            net_content_quantity=qty,
            net_content_unit=unit,
            unit_price=up_amount if (up_amount and up_unit) else None,
            unit_price_unit=up_unit if (up_amount and up_unit) else None,
            image_url=r.get("image") or None,
            promotion=None,
            verification_status=ProviderVerificationStatus.PROVIDER_REPORTED,
            confidence_score=Decimal("1.0"),
            raw_source_reference=(
                f"id:{r.get('retailerProductId')}; source_observed_at=absent; "
                f"retrieved_at={retrieved_at.isoformat()}; mapping={self.mapping_version}"
            ),
        )


# ---- Carrefour (dense catalogue, postal-scoped, unit price by weight) ---------------------- #


class ParseBotCarrefourMapper(_BaseParseBotMapper):
    provider_code = "parsebot-carrefour"
    retailer_slug = "carrefour"
    required_core = (
        "product_id",
        "name",
        "brand",
        "regular_price",
        "promotional_price",
        "loyalty_price",
        "measure_unit",
        "package_quantity",
        "package_unit",
        "net_content",
        "unit_price",
        "unit_price_unit",
        "availability",
        "ean",
        "postal_code",
        "sale_point",
        "observed_at",
        "promotion_text",
        "promotion_start_date",
        "promotion_end_date",
    )
    supported_schema_fingerprints = (
        "ef52b00a840410cd64485e0c281940f9504acadbae1ba73140729d945952ee4b",
    )
    _AVAIL: ClassVar[dict[str, Availability]] = {
        "in_stock": Availability.IN_STOCK,
        "out_of_stock": Availability.OUT_OF_STOCK,
    }

    def map_product(self, r: dict, retrieved_at: datetime) -> ExternalCatalogProduct:
        regular = _dec(r.get("regular_price"))
        promo = _dec(r.get("promotional_price"))
        if regular is None:
            regular = promo
        if regular is None:
            raise UnsupportedSchemaError("carrefour record without a decodable price")
        qty, unit = self._net_content(r)
        measure = str(r.get("measure_unit") or "").lower()
        variable = unit is None and measure in ("kg", "g", "l", "ml")
        up_unit = _UNIT_PRICE_UNITS.get(str(r.get("unit_price_unit") or "").lower())
        up = _dec(r.get("unit_price"))
        return ExternalCatalogProduct(
            provider=self.provider_code,
            retailer_slug=self.retailer_slug,
            external_product_id=str(r.get("product_id")),
            product_name=r.get("name") or "",
            brand=r.get("brand") or None,
            category=r.get("category") or None,
            barcode=r.get("ean") or None,
            sell_unit=SellUnit.WEIGHT if variable else SellUnit.PACKAGE,
            regular_price=regular,
            promotional_price=promo if (promo and promo < regular) else None,
            loyalty_price=_dec(r.get("loyalty_price")),
            currency="EUR",  # no currency field; firm ES postal_code -> EUR (recorded below)
            price_scope=PriceScope.POSTAL_CODE if r.get("postal_code") else PriceScope.UNKNOWN,
            observed_at=_to_dt(r.get("observed_at"), fallback=retrieved_at),
            availability=self._AVAIL.get(
                str(r.get("availability") or "").lower(), Availability.UNKNOWN
            ),
            variable_weight=variable,
            net_content_quantity=qty,
            net_content_unit=unit,
            unit_price=up if (up and up_unit) else None,
            unit_price_unit=up_unit if (up and up_unit) else None,
            external_store_id=str(r.get("sale_point")) if r.get("sale_point") else None,
            postal_code=str(r.get("postal_code")) if r.get("postal_code") else None,
            image_url=r.get("image_url") or None,
            product_url=r.get("product_url") or None,
            promotion=self._promo(r, promo, regular),
            verification_status=ProviderVerificationStatus.PROVIDER_REPORTED,
            confidence_score=Decimal("1.0"),
            raw_source_reference=(
                f"id:{r.get('product_id')}; currency=EUR(inferred:ES postal_code); "
                f"retrieved_at={retrieved_at.isoformat()}; mapping={self.mapping_version}"
            ),
        )

    def _net_content(self, r: dict) -> tuple[Decimal | None, ContentUnit | None]:
        qty, unit = _parse_content(r.get("net_content"))
        if qty is not None:
            return qty, unit
        pq = _dec(r.get("package_quantity"))
        pu = _UNIT_ALIASES.get(str(r.get("package_unit") or "").lower())
        if pq is not None and pu is not None:
            return pq, pu
        return None, None

    def _promo(self, r: dict, promo: Decimal | None, regular: Decimal) -> ProviderPromotion | None:
        if not (promo and promo < regular):
            return None
        pct = ((regular - promo) / regular * Decimal("100")).quantize(Decimal("0.01"))
        return ProviderPromotion(
            price_type=PriceType.PROMOTIONAL, promotional_price=promo, percentage_discount=pct
        )


# ---- Aldi (weekly offers: current vs previous price, textual size) ------------------------- #


class ParseBotAldiMapper(_BaseParseBotMapper):
    provider_code = "parsebot-aldi"
    retailer_slug = "aldi"
    required_core = (
        "product_id",
        "title",
        "brand",
        "category",
        "displayed_price",
        "previous_price",
        "package_size",
        "promotion_text",
        "region",
        "observation_timestamp",
        "valid_from",
        "valid_until",
        "image_url",
        "product_url",
    )
    supported_schema_fingerprints = (
        "d7fafc34f3cb3123d935fcfb2f0ac29b7b04ccd1c0a4c262dee3429dd9b3f5da",
    )

    def map_product(self, r: dict, retrieved_at: datetime) -> ExternalCatalogProduct:
        displayed = _dec(r.get("displayed_price"))
        previous = _dec(r.get("previous_price"))
        if displayed is None:
            raise UnsupportedSchemaError("aldi offer without a decodable price")
        regular: Decimal = displayed
        promotional: Decimal | None = None
        if previous is not None and previous > displayed:
            regular, promotional = previous, displayed
        qty, unit = _parse_content(r.get("package_size"))
        size_text = str(r.get("package_size") or "").lower()
        variable = "granel" in size_text or "kg" in size_text
        return ExternalCatalogProduct(
            provider=self.provider_code,
            retailer_slug=self.retailer_slug,
            external_product_id=str(r.get("product_id")),
            product_name=r.get("title") or "",
            brand=r.get("brand") or None,
            category=r.get("category") or None,
            barcode=None,
            sell_unit=SellUnit.WEIGHT if variable else SellUnit.PACKAGE,
            regular_price=regular,
            promotional_price=promotional,
            loyalty_price=None,
            currency="EUR",  # no currency field; firm ES source (region=peninsula) -> EUR
            price_scope=PriceScope.UNKNOWN,  # marketing region, not a pricing store/zone
            observed_at=_to_dt(r.get("observation_timestamp"), fallback=retrieved_at),
            availability=Availability.UNKNOWN,
            variable_weight=variable,
            net_content_quantity=qty,  # usually None: Aldi sizes are free text -> low coverage
            net_content_unit=unit,
            image_url=r.get("image_url") or None,
            product_url=r.get("product_url") or None,
            promotion=self._promo(r, promotional, regular),
            verification_status=ProviderVerificationStatus.PROVIDER_REPORTED,
            confidence_score=Decimal("1.0"),
            raw_source_reference=(
                f"id:{r.get('product_id')}; currency=EUR(inferred:ES); "
                f"valid={r.get('valid_from')}..{r.get('valid_until')}; "
                f"mapping={self.mapping_version}"
            ),
        )

    def _promo(self, r: dict, promo: Decimal | None, regular: Decimal) -> ProviderPromotion | None:
        if promo is None:
            return None
        return ProviderPromotion(
            price_type=PriceType.PROMOTIONAL,
            promotional_price=promo,
            percentage_discount=_pct(r.get("promotion_text")),
        )


# ---- Lidl (store-scoped visible products: explicit EUR + packaging) ------------------------ #


class ParseBotLidlMapper(_BaseParseBotMapper):
    provider_code = "parsebot-lidl"
    retailer_slug = "lidl"
    required_core = (
        "product_id",
        "name",
        "full_title",
        "brand",
        "category",
        "price",
        "currency",
        "old_price",
        "discount_percentage",
        "promotion",
        "packaging",
        "observed_at",
        "store_id",
        "image",
        "product_url",
    )
    supported_schema_fingerprints = (
        "34b73c46c3c060e33a213e7a33f82cc44318e67b113fbc90f8968aee76c87fa0",
    )

    def map_product(self, r: dict, retrieved_at: datetime) -> ExternalCatalogProduct:
        price = _dec(r.get("price"))
        if price is None:
            raise UnsupportedSchemaError("lidl product without a decodable price")
        old = _dec(r.get("old_price"))
        regular: Decimal = price
        promotional: Decimal | None = None
        if old is not None and old > price:
            regular, promotional = old, price
        qty, unit = _parse_content(r.get("packaging"))
        return ExternalCatalogProduct(
            provider=self.provider_code,
            retailer_slug=self.retailer_slug,
            external_product_id=str(r.get("product_id")),
            product_name=r.get("name") or r.get("full_title") or "",
            brand=r.get("brand") or None,
            category=r.get("category") or None,
            barcode=None,
            sell_unit=SellUnit.PACKAGE,
            regular_price=regular,
            promotional_price=promotional,
            loyalty_price=None,
            currency=r.get("currency") or "EUR",  # explicit in the response
            price_scope=PriceScope.EXACT_STORE if r.get("store_id") else PriceScope.UNKNOWN,
            observed_at=_to_dt(r.get("observed_at"), fallback=retrieved_at),
            availability=Availability.IN_STOCK,
            variable_weight=False,
            net_content_quantity=qty,
            net_content_unit=unit,
            external_store_id=str(r.get("store_id")) if r.get("store_id") else None,
            image_url=r.get("image") or None,
            product_url=r.get("product_url") or None,
            promotion=self._promo(r, promotional),
            verification_status=ProviderVerificationStatus.PROVIDER_REPORTED,
            confidence_score=Decimal("1.0"),
            raw_source_reference=(
                f"id:{r.get('product_id')}; store:{r.get('store_id')}; "
                f"retrieved_at={retrieved_at.isoformat()}; mapping={self.mapping_version}"
            ),
        )

    def _promo(self, r: dict, promo: Decimal | None) -> ProviderPromotion | None:
        if promo is None:
            return None
        return ProviderPromotion(
            price_type=PriceType.PROMOTIONAL,
            promotional_price=promo,
            percentage_discount=_dec(r.get("discount_percentage")),
        )


# ---- generic provider driving a chain via its capture plan + mapper ------------------------ #


class _ParseBotChainProvider(PriceCatalogProvider):
    """A Parse.bot chain provider: fetches via the chain's capture plan, maps via its mapper."""

    provider_code: str = ""
    retailer_slug: str = ""
    _catalog_type: str = "search_partial"
    _mapper_cls: type[_BaseParseBotMapper] = _BaseParseBotMapper
    _capabilities = ProviderCapabilities(full_catalog=False, promotions=True, search=True)
    _query = "leche"

    def __init__(self, *, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._mapper = self._mapper_cls()

    def capabilities(self) -> ProviderCapabilities:
        return self._capabilities

    def get_source_metadata(self) -> ProviderMetadata:
        return ProviderMetadata(
            provider_code=self.provider_code,
            retailer_slug=self.retailer_slug,
            kind=ProviderKind.INDEPENDENT,  # third-party scraper API, NOT the retailer's own API
            status=ProviderStatus.ACTIVE_WHEN_CONFIGURED,
            official=False,
            catalog_type=self._catalog_type,
            attribution=(
                f"Parse.bot (scraper de terceros). No es una API oficial de {self.retailer_slug}."
            ),
        )

    def _configured(self) -> bool:
        base = getattr(self._settings, plans.base_url_attr(self.provider_code), "")
        return bool(self._settings.parse_bot_api_key and base)

    def health_check(self) -> HealthStatus:
        if not self._configured():
            return HealthStatus(ok=False, detail=f"{self.provider_code} not configured")
        return HealthStatus(
            ok=True, detail=f"{self.provider_code} configured", checked_at=datetime.now(UTC)
        )

    def iterate_products(self, query: ProductQuery) -> Iterator[ExternalCatalogProduct]:
        if not self._configured():
            raise NotSupportedError(f"{self.provider_code} not configured (missing key/base URL)")
        limit = min(query.max_products or 10, 10)
        records = plans.capture_records(
            self.provider_code, self._settings, limit=limit, query=query.search or self._query
        )
        yield from self._mapper.map_products(records, retrieved_at=datetime.now(UTC))


class ParseBotAlcampoProvider(_ParseBotChainProvider):
    provider_code = "parsebot-alcampo"
    retailer_slug = "alcampo"
    _catalog_type = "search_dense_candidate"
    _mapper_cls = ParseBotAlcampoMapper
    _capabilities = ProviderCapabilities(
        full_catalog=False, promotions=True, categories=True, search=True
    )


class ParseBotCarrefourProvider(_ParseBotChainProvider):
    provider_code = "parsebot-carrefour"
    retailer_slug = "carrefour"
    _catalog_type = "category_dense_candidate"
    _mapper_cls = ParseBotCarrefourMapper
    _capabilities = ProviderCapabilities(
        full_catalog=False, store_scope=True, promotions=True, categories=True, search=True
    )


class ParseBotAldiProvider(_ParseBotChainProvider):
    provider_code = "parsebot-aldi"
    retailer_slug = "aldi"
    _catalog_type = "weekly_offers_partial"
    _mapper_cls = ParseBotAldiMapper
    _capabilities = ProviderCapabilities(full_catalog=False, promotions=True)


class ParseBotLidlProvider(_ParseBotChainProvider):
    provider_code = "parsebot-lidl"
    retailer_slug = "lidl"
    _catalog_type = "store_offers_partial"
    _mapper_cls = ParseBotLidlMapper
    _capabilities = ProviderCapabilities(full_catalog=False, store_scope=True, promotions=True)
    _query = "Madrid"


__all__ = [
    "ParseBotAlcampoMapper",
    "ParseBotAlcampoProvider",
    "ParseBotAldiMapper",
    "ParseBotAldiProvider",
    "ParseBotCarrefourMapper",
    "ParseBotCarrefourProvider",
    "ParseBotLidlMapper",
    "ParseBotLidlProvider",
    "UnsupportedSchemaError",
]
