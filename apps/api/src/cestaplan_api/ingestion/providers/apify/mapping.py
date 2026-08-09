"""Apify Mercadona mapper + provider (spec §5-§7) — grounded only in the observed capture.

This mapper consumes the ``igolaizola/mercadona-scraper`` Apify actor, whose records are RICHER
than the previous actor: they carry a structured ``price_instructions`` block with the real net
content (``unit_size`` + ``size_format``), so Mercadona products are costed as a normal
FIXED_PACKAGE — no unit-price workaround is needed (``apify-mercadona`` is deliberately NOT in
``quality.UNIT_PRICE_COSTED_PROVIDERS``).

Nothing is invented:

- ``barcode`` is ``None`` for every item — the actor exposes no EAN, and a barcode is never
  fabricated from the name (§7).
- ``currency`` is the constant ``"EUR"``: Mercadona is a Spanish retailer and the actor does not
  report a currency, so it is a documented constant, not a guessed field.
- PRICE comes from ``price_instructions``: ``unit_price`` is the real shelf price the customer pays.
  A markdown is only read when ``price_decreased`` is true AND ``previous_unit_price`` is present
  and genuinely above ``unit_price`` (regular := previous, promotional := unit_price); anything else
  is read as a plain regular price. Every money value is a :class:`~decimal.Decimal` parsed robustly
  from its string (es-ES separators tolerated); a malformed value raises, never a wrong number.
- NET CONTENT is structured: ``net_content_quantity := unit_size`` and ``net_content_unit`` is
  ``size_format`` normalized to :class:`ContentUnit` (l->L, kg->KG, ml->ML, g->G). When either is
  absent or the unit is unknown, BOTH stay ``None`` (never guessed) and the product falls back to
  being non-costable — the honest limit.
- a supplementary ``unit_price``/``unit_price_unit`` (price per L/kg) is taken from
  ``reference_price`` + ``reference_format``; when it does not parse cleanly it is dropped.
- ``category`` is the MOST SPECIFIC (deepest) node of the ``categories`` tree — the leaf carries the
  most useful signal for ingredient matching; ``None`` when the tree is empty.
- ``availability``: ``published`` and no active ``unavailable_from``/``unavailable_weekdays`` ->
  IN_STOCK; a future/again unavailability window -> LIMITED; unpublished or an active
  ``unavailable_from`` -> OUT_OF_STOCK.
- ``observed_at`` is the RETRIEVAL time threaded by the provider: this actor emits no source
  timestamp (unlike the previous one's ``scrapedAt``), so ``source_observed_at`` is recorded as
  absent in ``raw_source_reference``.
- ``price_scope`` is ``postal_code``: Mercadona prices vary by delivery zone. The provider runs
  with one postal code (``apify_mercadona_default_postal_code``) and threads it here, stamping it on
  every product. Without a postal code the scope is ``UNKNOWN`` (no zone -> price unlocatable).
- an unknown schema fingerprint blocks normalization.

Schema pinning: the fingerprint is computed over the REQUIRED core — the always-present,
stable-typed fields the mapper depends on: top-level ``id``/``display_name``/``share_url``/
``published`` and, inside ``price_instructions``, ``unit_price``/``unit_size``/``size_format``/
``reference_price``. Variable/nullable fields (``previous_unit_price``, ``thumbnail``,
``packaging``, ``categories``, the promo flags) are deliberately EXCLUDED so a heterogeneous real
capture never blocks the batch, while structural drift in a depended-on core field still blocks.
``unit_size`` is a JSON number sometimes an int (``5``) and sometimes a float (``0.75``); it is
normalized to a canonical float in the core projection so that harmless int/float variance across a
batch never shifts the fingerprint, while a genuine type change (e.g. to a string) still blocks.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, BeforeValidator, ConfigDict

from cestaplan_api.config import Settings, get_settings
from cestaplan_api.ingestion.contracts import PriceScope, PriceType
from cestaplan_api.ingestion.providers.apify.client import ApifyClient
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
from cestaplan_api.ingestion.providers.schema_tools import merge_samples, schema_fingerprint

# ``size_format`` (net-content unit) tokens -> our ContentUnit. Unknown -> net content dropped.
_SIZE_FORMAT_UNITS = {
    "l": ContentUnit.L,
    "kg": ContentUnit.KG,
    "ml": ContentUnit.ML,
    "g": ContentUnit.G,
}
# ``reference_format`` (€/unit) tokens -> our unit code for the supplementary unit price.
_REFERENCE_FORMAT_UNITS = {
    "l": "l",
    "kg": "kg",
    "ml": "ml",
    "g": "g",
}
# The always-present core the mapper depends on (see module docstring). Top-level + a projection of
# the price_instructions block; everything else is legitimately nullable/variable and excluded.
_TOP_CORE = ("id", "display_name", "share_url", "published")
_PRICE_INSTRUCTIONS_CORE = ("unit_price", "unit_size", "size_format", "reference_price")


# A clean numeric literal after separators are normalised: optional sign, digits, optional
# single decimal group. Anything else is refused (never silently coerced to a wrong number).
_NUMERIC_RE = re.compile(r"^-?\d+(?:\.\d+)?$")


class InvalidMoneyValue(ValueError):
    """A money string that cannot be parsed to a clean Decimal (surfaced, never guessed)."""


def _decimal_from_str(raw: str) -> Decimal:
    """Parse a money string to Decimal, tolerating a currency symbol and es-ES separators.

    Handles ``"5,04"`` (comma decimal), ``"5,04 €"`` (currency symbol), ``"1.234,56"`` (dot
    thousands + comma decimal) and ``"1,234.56"`` (comma thousands + dot decimal), as well as the
    leading whitespace the actor sometimes emits (``"       18.75"``). Anything that is not a clean
    number after normalisation raises :class:`InvalidMoneyValue` — a malformed price is NEVER
    coerced into a wrong number.
    """
    cleaned = re.sub(r"[^\d.,\-]", "", raw.strip())  # drop currency symbol / spaces / letters
    if "." in cleaned and "," in cleaned:
        # The RIGHTMOST separator is the decimal; the other one groups thousands.
        if cleaned.rfind(",") > cleaned.rfind("."):
            cleaned = cleaned.replace(".", "").replace(",", ".")  # 1.234,56 -> 1234.56
        else:
            cleaned = cleaned.replace(",", "")  # 1,234.56 -> 1234.56
    elif "," in cleaned:
        cleaned = cleaned.replace(",", ".")  # 5,04 -> 5.04
    if not _NUMERIC_RE.fullmatch(cleaned):
        raise InvalidMoneyValue(f"invalid money value: {raw!r}")
    return Decimal(cleaned)


def _to_decimal(value: object) -> object:
    # money arrives as a string with es-ES separators / a currency symbol, or (for unit_size) as a
    # JSON number; parse robustly and go through str to avoid float imprecision.
    if value is None or isinstance(value, Decimal):
        return value
    if isinstance(value, str):
        return _decimal_from_str(value)
    return Decimal(str(value))


Money = Annotated[Decimal, BeforeValidator(_to_decimal)]


class UnsupportedSchemaError(ProviderError):
    """The batch's schema fingerprint is not one the mapper is validated against."""


class ApifyMercadonaPriceInstructions(BaseModel):
    """The ``price_instructions`` block — the only place price/net-content live.

    Only the depended-on core (``unit_price``/``unit_size``/``size_format``/``reference_price``) is
    required; the promo fields and ``reference_format`` are nullable so a heterogeneous capture
    never blocks the batch.
    """

    model_config = ConfigDict(extra="ignore")

    unit_price: Money
    unit_size: Money  # JSON number (net content in size_format units) -> Decimal via str
    size_format: str
    reference_price: str  # parsed leniently in the mapper (a bad value -> no supplementary price)
    reference_format: str | None = None
    previous_unit_price: str | None = None
    price_decreased: bool = False


class ApifyMercadonaRecord(BaseModel):
    """One ``igolaizola/mercadona-scraper`` dataset item — derived ONLY from the observed capture.

    Only the depended-on core is required; every other field is nullable so a real, heterogeneous
    capture never blocks the batch. ``extra="ignore"`` tolerates new fields without failing.
    """

    model_config = ConfigDict(extra="ignore")

    id: str
    display_name: str
    share_url: str
    published: bool
    price_instructions: ApifyMercadonaPriceInstructions
    # Nullable / non-core fields.
    thumbnail: str | None = None
    categories: list[dict] | None = None
    unavailable_from: str | None = None
    unavailable_weekdays: list | None = None


def _deepest_category_name(categories: list[dict] | None) -> str | None:
    """The name of the deepest (most specific) node in the ``categories`` tree, or None if empty.

    The leaf carries the most useful signal for ingredient matching; ties keep the first branch.
    """
    if not categories:
        return None
    best_name: str | None = None
    best_level = -1

    def walk(node: dict) -> None:
        nonlocal best_name, best_level
        level = node.get("level")
        name = node.get("name")
        if isinstance(level, int) and level > best_level and isinstance(name, str) and name:
            best_level, best_name = level, name
        children = node.get("categories")
        if isinstance(children, list):
            for child in children:
                if isinstance(child, dict):
                    walk(child)

    for root in categories:
        if isinstance(root, dict):
            walk(root)
    return best_name


def _net_content(
    quantity: Decimal, size_format: str
) -> tuple[Decimal, ContentUnit] | tuple[None, None]:
    """Structured net content from ``unit_size`` + ``size_format``; (None, None) if unit unknown."""
    unit = _SIZE_FORMAT_UNITS.get(size_format.strip().lower())
    if unit is None or quantity <= 0:
        return None, None
    return quantity, unit


def _reference_unit_price(
    reference_price: str, reference_format: str | None
) -> tuple[Decimal | None, str | None]:
    """Supplementary €/unit price from ``reference_price`` + ``reference_format``.

    Returns ``(None, None)`` when the price does not parse to a positive Decimal or the unit is
    unknown — the mapper never guesses.
    """
    if not reference_format:
        return None, None
    unit = _REFERENCE_FORMAT_UNITS.get(reference_format.strip().lower())
    if unit is None:
        return None, None
    try:
        value = _decimal_from_str(reference_price)
    except InvalidMoneyValue:
        return None, None
    if value <= 0:
        return None, None
    return value, unit


class ApifyMercadonaMapper:
    mapping_version = "2.0.0"  # igolaizola schema (structured net content); was 1.0.0 (studio-amba)
    retailer_slug = "mercadona"
    provider_code = "apify-mercadona"
    # Fingerprint of the required-field core observed in the sanitized igolaizola Mercadona sample.
    supported_schema_fingerprints = (
        "ddb7440cf6358ed7c6a9f114d56ae59531ab1b4c233bd3fb57fc70b002bade7c",
    )

    def _core(self, record: dict) -> dict:
        """Project a record to the required core; ``unit_size`` normalized to a canonical float.

        Only present keys are included (a missing optional never alters the structure).
        ``unit_size`` is a JSON number that is sometimes int, sometimes float; casting it to float
        pins it as "numeric" so harmless int/float variance across the batch never shifts the
        fingerprint, while a genuine type change (e.g. to a string) still changes it and blocks.
        """
        core: dict = {k: record[k] for k in _TOP_CORE if k in record}
        pi = record.get("price_instructions")
        if isinstance(pi, dict):
            projected = {k: pi[k] for k in _PRICE_INSTRUCTIONS_CORE if k in pi}
            size = projected.get("unit_size")
            if isinstance(size, (int, float)) and not isinstance(size, bool):
                projected["unit_size"] = float(size)
            core["price_instructions"] = projected
        return core

    def detect_schema(self, records: list[dict]) -> str:
        """Fingerprint of the required-field core (stable to nullable-field variance)."""
        return schema_fingerprint(merge_samples([self._core(r) for r in records]))

    def validate_supported_schema(self, records: list[dict]) -> str:
        fp = self.detect_schema(records)
        if fp not in self.supported_schema_fingerprints:
            raise UnsupportedSchemaError(
                f"unknown Mercadona schema fingerprint {fp}; capture + review before mapping"
            )
        return fp

    def map_products(
        self, records: list[dict], *, postal_code: str | None = None, observed_at: datetime
    ) -> list[ExternalCatalogProduct]:
        if not records:  # empty response -> nothing to normalize (not an error)
            return []
        self.validate_supported_schema(records)  # unknown fingerprint blocks normalization
        return [
            self.map_product(
                ApifyMercadonaRecord.model_validate(r),
                postal_code=postal_code,
                observed_at=observed_at,
            )
            for r in records
        ]

    def map_product(
        self,
        record: ApifyMercadonaRecord,
        *,
        postal_code: str | None = None,
        observed_at: datetime,
    ) -> ExternalCatalogProduct:
        pi = record.price_instructions
        regular, promotional = self._prices(pi)
        net_qty, net_unit = _net_content(pi.unit_size, pi.size_format)
        unit_price, unit_price_unit = _reference_unit_price(pi.reference_price, pi.reference_format)
        return ExternalCatalogProduct(
            provider=self.provider_code,
            retailer_slug=self.retailer_slug,
            external_product_id=record.id,
            product_name=record.display_name,
            brand=None,  # actor exposes no brand field — never inferred from the name
            category=_deepest_category_name(record.categories),
            barcode=None,  # no EAN in this actor — never invented
            sell_unit=SellUnit.PACKAGE,  # sold as a package with a known net content
            regular_price=regular,
            promotional_price=promotional,
            currency="EUR",  # Mercadona (Spain); actor reports no currency -> documented constant
            price_scope=self.map_scope(postal_code),  # postal_code (zone) — see map_scope
            postal_code=postal_code or None,
            observed_at=observed_at,  # retrieval time; this actor provides no source timestamp
            availability=self.map_availability(record),
            variable_weight=False,
            net_content_quantity=net_qty,  # structured net content -> FIXED_PACKAGE costing
            net_content_unit=net_unit,
            unit_price=unit_price,  # supplementary €/L or €/kg reference price
            unit_price_unit=unit_price_unit,
            image_url=record.thumbnail or None,
            product_url=record.share_url or None,
            promotion=self.map_promotion(pi),
            verification_status=ProviderVerificationStatus.PROVIDER_REPORTED,
            confidence_score=Decimal("1.0"),
            raw_source_reference=(
                f"id:{record.id}; source_observed_at=absent; "
                f"retrieved_at={observed_at.isoformat()}; postal_code={postal_code or 'none'}; "
                f"mapping={self.mapping_version}"
            ),
        )

    def _prices(
        self, pi: ApifyMercadonaPriceInstructions
    ) -> tuple[Decimal, Decimal | None]:
        """Return ``(regular, promotional)``.

        A markdown is only read when ``price_decreased`` is true AND ``previous_unit_price`` is
        present and genuinely above ``unit_price`` (regular := previous, promotional := unit_price);
        anything else is a plain regular price. An unparseable ``previous_unit_price`` is ignored
        (never read as a markdown), so a malformed field can never invert the price.
        """
        current = pi.unit_price
        if pi.price_decreased and pi.previous_unit_price:
            try:
                previous = _decimal_from_str(pi.previous_unit_price)
            except InvalidMoneyValue:
                return current, None
            if previous > current > 0:
                return previous, current
        return current, None

    def map_promotion(self, pi: ApifyMercadonaPriceInstructions) -> ProviderPromotion | None:
        regular, promotional = self._prices(pi)
        if promotional is None:
            return None
        percentage = ((regular - promotional) / regular * Decimal("100")).quantize(Decimal("0.01"))
        return ProviderPromotion(
            price_type=PriceType.PROMOTIONAL,
            promotional_price=promotional,
            percentage_discount=percentage,
        )

    def map_availability(self, record: ApifyMercadonaRecord) -> Availability:
        # Unpublished or an active unavailability date -> out of stock; a weekday-restricted window
        # -> limited; otherwise in stock. Conservative reads of the actor's availability signals.
        if not record.published or record.unavailable_from:
            return Availability.OUT_OF_STOCK
        if record.unavailable_weekdays:
            return Availability.LIMITED
        return Availability.IN_STOCK

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
            search=False,  # actor input is a bounded run, not a search query
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
        # Actor input: ``igolaizola/mercadona-scraper`` ignores ``maxItems`` (it returns the full
        # catalogue for the zone), but we pass it anyway as a harmless intent hint. The zone/
        # warehouse is selected via ``postalCode`` — an ASSUMED key: the actor's example run input
        # was a placeholder, so if the actor ignores it the scope simply reflects the actor's
        # default warehouse (zone sealing downstream is unchanged either way). Empty postal omitted.
        run_input: dict[str, object] = {"maxItems": limit}
        if postal_code:
            run_input["postalCode"] = postal_code
        run_id = self._client.start_run(
            self._settings.apify_mercadona_actor_id,
            run_input,
            max_total_charge_usd=self._settings.apify_max_total_charge_usd,
        )
        run = self._client.wait_for_run(run_id)
        records = self._client.get_dataset_items(str(run["defaultDatasetId"]), limit=limit)[:limit]
        observed_at = datetime.now(UTC)
        yield from self._mapper.map_products(
            records, postal_code=postal_code or None, observed_at=observed_at
        )


__all__ = [
    "ApifyMercadonaMapper",
    "ApifyMercadonaPriceInstructions",
    "ApifyMercadonaProvider",
    "ApifyMercadonaRecord",
    "UnsupportedSchemaError",
]
