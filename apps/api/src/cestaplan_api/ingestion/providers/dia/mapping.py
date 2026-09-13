"""DIA direct-API mapper — one ``search_item`` -> the normalized :class:`ExternalCatalogProduct`.

Consumes records under provider code ``parsebot-dia`` (the code is PRESERVED across the switch from
the Parse.bot scraper to DIA's direct public API — the rights registry and existing rows key on it,
exactly like Mercadona kept ``apify-mercadona``). Nothing is invented:

- ``external_product_id`` is ``sku_id`` (equal to ``object_id`` in the observed contract).
- ``product_name`` is ``display_name``; ``brand`` is ``brand`` (empty -> ``None``, never inferred).
- ``barcode`` is always ``None`` — DIA's search response carries no EAN (§7).
- PRICE comes from ``prices.price`` (the shelf price). A markdown is read only when
  ``is_promo_price`` is true AND ``strikethrough_price`` is genuinely above ``price`` (regular :=
  strikethrough, promotional := price); anything else is a plain regular price. A non-positive price
  is REFUSED (:class:`NonPositivePriceError`), never coerced into a free/negative cost.
- NET CONTENT is parsed from ``display_name`` ("1 L", "500 g", "pack 6 x 1 L" -> 6 L). When
  parseable it drives normal FIXED_PACKAGE costing. When it is NOT parseable, net content stays
  ``None`` and the product falls back to UNIT-PRICE costing from ``price_per_unit`` +
  ``measure_unit`` (LITRO->l, KILO->kg, UNIDAD->unit) — ``parsebot-dia`` is a member of
  ``quality.UNIT_PRICE_COSTED_PROVIDERS``, so a unit-price-only DIA product is still costable.
- ``price_scope`` is ``NATIONAL``: v1 reads DIA's DEFAULT national warehouse (``cart.postal_code``
  28041); the response carries no per-store/zone split. Zone-pinning is a documented follow-up.
- ``observed_at`` is the RETRIEVAL time threaded by the provider; the source emits no timestamp, so
  ``source_observed_at`` is recorded as absent in ``raw_source_reference``.

Tolerance (PER-PRODUCT, never per-batch): :meth:`DiaMapper.map_products` maps each record
INDEPENDENTLY and DROPS the ones that do not fit (a missing/ill-typed core field, a malformed or
non-positive price) instead of blocking the whole batch. Skipped records produce no data; the count
is exposed on ``last_skipped_count`` and logged, so lost coverage is visible, never silent.
Principle: reduce coverage, NEVER invent.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation

from pydantic import BaseModel, ConfigDict, ValidationError

from cestaplan_api.ingestion.contracts import PriceScope, PriceType
from cestaplan_api.ingestion.providers.contracts import (
    Availability,
    ContentUnit,
    ExternalCatalogProduct,
    ProviderPromotion,
    ProviderVerificationStatus,
    SellUnit,
)
from cestaplan_api.ingestion.providers.exceptions import ProviderError

logger = logging.getLogger(__name__)

# Net-content unit tokens (as they appear in ``display_name``) -> our ContentUnit. Only exact
# mass/volume units are supported; anything else (e.g. "cl", "unidad") -> net content dropped, so
# the product falls to unit-price costing rather than being mis-costed. Nothing is converted.
_NET_CONTENT_UNITS = {
    "l": ContentUnit.L,
    "litro": ContentUnit.L,
    "litros": ContentUnit.L,
    "ml": ContentUnit.ML,
    "mililitro": ContentUnit.ML,
    "mililitros": ContentUnit.ML,
    "g": ContentUnit.G,
    "gr": ContentUnit.G,
    "gramo": ContentUnit.G,
    "gramos": ContentUnit.G,
    "kg": ContentUnit.KG,
    "kilo": ContentUnit.KG,
    "kilos": ContentUnit.KG,
    "kilogramo": ContentUnit.KG,
    "kilogramos": ContentUnit.KG,
}
# ``prices.measure_unit`` words DIA uses for the €/unit reference -> our unit code. Unknown -> the
# unit price is dropped (never guessed).
_MEASURE_UNIT_ALIASES = {
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

_UNIT_TOKEN = "l|ml|g|gr|kg|litros?|mililitros?|gramos?|kilos?|kilogramos?"
# "pack 6 x 1 L" / "6 x 1 L" / "6x1L" -> count x per-quantity + unit (total = count * per).
_PACK_RE = re.compile(
    rf"(\d+)\s*[x×]\s*(\d+(?:[.,]\d+)?)\s*({_UNIT_TOKEN})\b",  # noqa: RUF001 (U+00D7 intended)
    re.IGNORECASE,
)
# A single "<qty> <unit>" occurrence (net content usually sits at the END of the name).
_SINGLE_RE = re.compile(rf"(\d+(?:[.,]\d+)?)\s*({_UNIT_TOKEN})\b", re.IGNORECASE)


class NonPositivePriceError(ValueError):
    """A price that parsed cleanly but is <= 0 — refused, never coerced into a free/negative cost.

    In :meth:`DiaMapper.map_products` this SKIPS just that product (counted and logged), so an
    anomalous price is dropped rather than emitted as a 0 €/negative observation, and never blocks
    the rest of the batch."""


class UnsupportedSchemaError(ProviderError):
    """Retained for parity with the sibling mappers; NOT raised by the per-product-tolerant
    :meth:`DiaMapper.map_products`, which skips drifting records instead of blocking the batch."""


class DiaPrices(BaseModel):
    """The ``prices`` block — the only place price/unit-price live. Only ``price`` is required."""

    model_config = ConfigDict(extra="ignore")

    price: float
    price_per_unit: float | None = None
    measure_unit: str | None = None
    strikethrough_price: float | None = None
    is_promo_price: bool = False
    discount_percentage: float | int | None = None
    currency: str = "EUR"


class DiaRecord(BaseModel):
    """One DIA ``search_item`` — only the depended-on core is required; the rest is nullable so a
    heterogeneous capture never blocks the batch. ``extra="ignore"`` tolerates new fields."""

    model_config = ConfigDict(extra="ignore")

    sku_id: str
    display_name: str
    prices: DiaPrices
    brand: str | None = None
    object_id: str | None = None
    units_in_stock: int | None = None
    url: str | None = None
    image: str | None = None
    l1_category_description: str | None = None
    l2_category_description: str | None = None


def _to_decimal(value: float | int | Decimal) -> Decimal:
    """A money/number value -> Decimal via ``str`` (never a float) to avoid float imprecision."""
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _qty_to_decimal(raw: str) -> Decimal:
    """A regex-captured quantity ("1", "0,75", "1.5") -> Decimal (es-ES comma tolerated)."""
    return Decimal(raw.replace(",", "."))


def _net_content_from_name(name: str) -> tuple[Decimal, ContentUnit] | tuple[None, None]:
    """Structured net content parsed from ``display_name``; (None, None) when not cleanly parseable.

    A pack pattern ("pack 6 x 1 L") multiplies count x per-quantity; otherwise the LAST single
    "<qty> <unit>" occurrence is used (net content sits at the end of DIA names). An unknown unit or
    a non-positive quantity drops the net content (the product then falls to unit-price costing).
    """
    pack = _PACK_RE.search(name)
    if pack is not None:
        unit = _NET_CONTENT_UNITS.get(pack.group(3).lower())
        if unit is None:
            return None, None
        try:
            count = _qty_to_decimal(pack.group(1))
            per = _qty_to_decimal(pack.group(2))
        except InvalidOperation:
            return None, None
        total = count * per
        return (total, unit) if count > 0 and per > 0 else (None, None)
    matches = list(_SINGLE_RE.finditer(name))
    if not matches:
        return None, None
    last = matches[-1]
    unit = _NET_CONTENT_UNITS.get(last.group(2).lower())
    if unit is None:
        return None, None
    try:
        qty = _qty_to_decimal(last.group(1))
    except InvalidOperation:
        return None, None
    return (qty, unit) if qty > 0 else (None, None)


class DiaMapper:
    mapping_version = "1.0.0"
    retailer_slug = "dia"
    provider_code = "parsebot-dia"  # preserved: rights registry + existing rows key off it
    # Telemetry: how many records the last ``map_products`` call SKIPPED (a per-product anomaly).
    # Read by the sync so lost coverage is never hidden. Reset each call.
    last_skipped_count = 0

    def map_products(
        self, records: list[dict], *, observed_at: datetime
    ) -> list[ExternalCatalogProduct]:
        """Map each record INDEPENDENTLY, skipping (never raising on) the ones that do not fit."""
        self.last_skipped_count = 0
        if not records:  # empty response -> nothing to normalize (not an error)
            return []
        products: list[ExternalCatalogProduct] = []
        skipped = 0
        for record in records:
            product = self._map_one(record, observed_at=observed_at)
            if product is None:
                skipped += 1
                continue
            products.append(product)
        self.last_skipped_count = skipped
        if skipped:
            logger.warning(
                "parsebot-dia: skipped %d of %d records (anomaly); mapped %d "
                "— coverage reduced, never invented",
                skipped,
                len(records),
                len(products),
            )
        return products

    def _map_one(self, record: dict, *, observed_at: datetime) -> ExternalCatalogProduct | None:
        """Map a single record, or return ``None`` (SKIP) when it does not fit.

        Skipped when: it is not a mapping; it fails pydantic validation (a missing/ill-typed core
        field); or a field cannot be parsed safely (a non-positive price). The skip is a no-op — it
        produces no data — so heterogeneity never blocks the sync and never yields a guessed value.
        """
        if not isinstance(record, dict):
            logger.debug("parsebot-dia: skipping non-mapping record %r", type(record).__name__)
            return None
        try:
            validated = DiaRecord.model_validate(record)
            return self.map_product(validated, observed_at=observed_at)
        except (ValidationError, ValueError, ArithmeticError) as exc:
            logger.debug("parsebot-dia: skipping record %r: %s", record.get("sku_id"), exc)
            return None

    def map_product(self, record: DiaRecord, *, observed_at: datetime) -> ExternalCatalogProduct:
        prices = record.prices
        regular, promotional = self._prices(prices)
        net_qty, net_unit = _net_content_from_name(record.display_name)
        unit_price, unit_price_unit = self._unit_price(prices)
        return ExternalCatalogProduct(
            provider=self.provider_code,
            retailer_slug=self.retailer_slug,
            external_product_id=record.sku_id,
            product_name=record.display_name,
            brand=record.brand or None,  # empty brand -> None, never inferred from the name
            category=record.l2_category_description or None,
            barcode=None,  # no EAN in DIA search — never invented
            sell_unit=SellUnit.PACKAGE,
            regular_price=regular,
            promotional_price=promotional,
            currency=prices.currency or "EUR",  # taken from the response, EUR is the safe default
            price_scope=PriceScope.NATIONAL,  # v1: DIA's default national warehouse
            postal_code=None,  # national scope -> no per-zone postal stamped (zone-pinning is TODO)
            observed_at=observed_at,  # retrieval time; source provides none
            availability=self._availability(record),
            variable_weight=False,
            net_content_quantity=net_qty,  # parseable -> FIXED_PACKAGE costing
            net_content_unit=net_unit,
            unit_price=unit_price,  # fallback €/L or €/kg or €/unit costing when net content absent
            unit_price_unit=unit_price_unit,
            image_url=record.image or None,
            product_url=record.url or None,
            promotion=self._promotion(regular, promotional),
            verification_status=ProviderVerificationStatus.PROVIDER_REPORTED,
            confidence_score=Decimal("1.0"),
            raw_source_reference=(
                f"sku:{record.sku_id}; source_observed_at=absent; "
                f"retrieved_at={observed_at.isoformat()}; scope=national; "
                f"mapping={self.mapping_version}"
            ),
        )

    def _prices(self, prices: DiaPrices) -> tuple[Decimal, Decimal | None]:
        """Return ``(regular, promotional)`` with ``regular`` strictly positive.

        A markdown is read only when ``is_promo_price`` is true AND ``strikethrough_price`` is
        genuinely above ``price`` (regular := strikethrough, promotional := price). A non-positive
        shelf price is refused with :class:`NonPositivePriceError`.
        """
        current = _to_decimal(prices.price)
        if current <= 0:
            raise NonPositivePriceError(f"non-positive price: {current}")
        if prices.is_promo_price and prices.strikethrough_price is not None:
            strike = _to_decimal(prices.strikethrough_price)
            if strike > current > 0:
                return strike, current
        return current, None

    def _unit_price(self, prices: DiaPrices) -> tuple[Decimal | None, str | None]:
        """Supplementary/fallback €/unit price from ``price_per_unit`` + ``measure_unit``.

        Dropped to ``(None, None)`` when the unit is unknown or the price-per-unit is not positive.
        """
        if not prices.measure_unit or prices.price_per_unit is None:
            return None, None
        unit = _MEASURE_UNIT_ALIASES.get(prices.measure_unit.strip().lower())
        if unit is None:
            return None, None
        value = _to_decimal(prices.price_per_unit)
        if value <= 0:
            return None, None
        return value, unit

    def _promotion(self, regular: Decimal, promotional: Decimal | None) -> ProviderPromotion | None:
        if promotional is None:
            return None
        percentage = ((regular - promotional) / regular * Decimal("100")).quantize(Decimal("0.01"))
        return ProviderPromotion(
            price_type=PriceType.PROMOTIONAL,
            promotional_price=promotional,
            percentage_discount=percentage,
        )

    def _availability(self, record: DiaRecord) -> Availability:
        if record.units_in_stock is None:
            return Availability.UNKNOWN
        return Availability.IN_STOCK if record.units_in_stock > 0 else Availability.OUT_OF_STOCK


__all__ = [
    "DiaMapper",
    "DiaPrices",
    "DiaRecord",
    "NonPositivePriceError",
    "UnsupportedSchemaError",
]
