"""OpenPricesConnector — the first **real** connector on the FASE A/B ingestion pipeline.

Open Food Facts **Open Prices** is a legal, open (ODbL) dataset of *real*, community-observed
prices, addressable by OpenStreetMap store location. No scraping, no robots concerns, no
anti-bot evasion and no fabrication: a price is only ever what the public API returns.

This connector reframes the pre-existing Open Prices integration
(:mod:`cestaplan_api.adapters.openprices` + :mod:`cestaplan_api.services.open_prices_sync`) so
it flows through the connector contract and the whole ingestion vertical
(discover -> fetch -> parse -> normalize -> validate -> record -> coverage -> project).

It does **not** duplicate any HTTP/parse logic: all network work is delegated to the injectable
:class:`~cestaplan_api.adapters.openprices.OpenPricesAdapter` (which already paginates, parses
each row to a :class:`~cestaplan_api.adapters.openprices.OpenPrice`, and degrades gracefully on
404/network/malformed payloads), and normalization/validation reuse the shared ingestion
helpers. Every Open Prices price is tied to one OSM location, so its scope is ``exact_store``.

Honesty invariants:
- ``full_catalog=False`` / ``partial_catalog=True`` — Open Prices is crowdsourced and sparse.
- ``promotions=False`` — real observed prices only; promotions are never fabricated
  (``price_type`` is always ``regular``, even for rows the source flags as discounted).
- A row without a usable barcode is skipped, never invented; a missing price stays missing.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from decimal import Decimal

from cestaplan_api.adapters.openprices import (
    OP_ADAPTER_KEY,
    OP_API_BASE,
    OP_ATTRIBUTION_TEXT,
    OP_DATA_SOURCE_SLUG,
    OP_LICENSE_CODE,
    OP_USER_AGENT,
    OpenPrice,
    OpenPricesAdapter,
)
from cestaplan_api.ingestion import (
    Capabilities,
    ConnectorStatus,
    FetchResult,
    HealthResult,
    LegalStatus,
    NormalizedObservation,
    ParseResult,
    PriceScope,
    PriceType,
    RetailerConnector,
    SourcePolicy,
    SourceRef,
    StoreResolutionResult,
    ValidationResult,
)
from cestaplan_api.ingestion.normalization import NormalizationError, PriceNormalizer
from cestaplan_api.ingestion.validation import ObservationValidator, ValidationContext
from cestaplan_api.services.open_prices_sync import (
    parse_osm_from_external_code,
    store_external_code,
)

logger = logging.getLogger(__name__)

#: Confidence carried by an open-dataset (crowdsourced) observation — matches the sync service.
_OP_CONFIDENCE = Decimal("0.5000")


def _package_from_price_per(price_per: str | None) -> tuple[str | None, str | None]:
    """Map an Open Prices ``price_per`` basis to ``(package_quantity, package_unit)``.

    Open Prices reports a per-kilogram / per-unit basis for loose items; packaged (barcoded)
    items usually carry none. When a basis is present the amount is a per-base price, so a
    package of one base unit lets the normalizer derive a coherent ``€/kg`` · ``€/unit`` unit
    price. An unknown basis yields ``(None, None)`` — no unit price is ever fabricated.
    """
    basis = (price_per or "").strip().upper()
    if basis in ("KILOGRAM", "KG"):
        return "1", "kg"
    if basis == "UNIT":
        return "1", "unit"
    return None, None


def _parse_iso_date(value: object) -> date | None:
    if isinstance(value, date):
        return value
    text = value.strip() if isinstance(value, str) else ""
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _clean(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


@dataclass(frozen=True, slots=True)
class OpenPricesParsedRecord:
    """A parsed Open Prices row: raw fields extracted from a capture, not yet normalized."""

    barcode: str
    name: str
    amount: str
    currency: str
    observed_on: date
    source_url: str | None
    package_quantity: str | None
    package_unit: str | None


class OpenPricesConnector(RetailerConnector):
    """A real :class:`RetailerConnector` over the Open Food Facts Open Prices dataset (ODbL).

    Scoped to one OSM store location (``osm_id`` / ``osm_type``, e.g. resolved from a
    :class:`~cestaplan_api.models.Store` ``external_code`` of ``osm:{TYPE}/{id}``). All prices
    it emits are ``exact_store`` scoped to that location. The underlying
    :class:`OpenPricesAdapter` is injectable so tests drive it with a mocked ``httpx`` client —
    no live network. Gated by ``enabled`` (the caller passes the Open Prices
    ``DataSource.is_enabled`` state); a disabled connector discovers nothing and reports
    ``DISABLED``.
    """

    retailer_code = OP_ADAPTER_KEY  # "open_prices"
    connector_version = "1.0.0"
    parser_version = "1.0.0"

    #: ODbL provenance surfaced for consumers (SourcePolicy has no license field).
    license_code = OP_LICENSE_CODE
    attribution_text = OP_ATTRIBUTION_TEXT

    def __init__(
        self,
        *,
        osm_id: int | None = None,
        osm_type: str | None = None,
        adapter: OpenPricesAdapter | None = None,
        enabled: bool = True,
    ) -> None:
        self._osm_id = osm_id
        self._osm_type = osm_type.upper() if osm_type else None
        self._adapter = adapter or OpenPricesAdapter()
        self._enabled = enabled
        self._price_normalizer = PriceNormalizer()
        self._validator = ObservationValidator()
        self._loaded = False
        self._by_barcode: dict[str, OpenPrice] = {}

    # -- required ------------------------------------------------------------ #
    def capabilities(self) -> Capabilities:
        """Open Prices: real prices with barcodes, exact-store scoped, but a *partial* catalog."""
        return Capabilities(
            prices=True,
            partial_catalog=True,
            full_catalog=False,  # crowdsourced and sparse — honest.
            barcodes=True,
            exact_store_scope=True,
            # Honest about everything Open Prices does NOT provide:
            promotions=False,
            loyalty_prices=False,
            availability=False,
            delivery_zone_scope=False,
            regional_scope=False,
            national_scope=False,
            product_images=False,
            nutrition=False,
            incremental_sync=False,
        )

    def source_policy(self) -> SourcePolicy:
        """A public, robots-respecting open dataset with an identifiable User-Agent (ODbL)."""
        return SourcePolicy(
            allowed_domains=("prices.openfoodfacts.org",),
            request_delay=1.0,
            max_concurrency=1,
            respects_robots=True,
            legal_status=LegalStatus.PUBLIC,
            contact=OP_USER_AGENT,
        )

    # -- health -------------------------------------------------------------- #
    def health_check(self) -> HealthResult:
        """Report the connector's operational state (enabled/disabled); no live probe."""
        if not self._enabled:
            return HealthResult(
                status=ConnectorStatus.DISABLED,
                ok=False,
                supported=True,
                checked_at=datetime.now(UTC),
                detail="Open Prices source disabled",
            )
        return HealthResult(
            status=ConnectorStatus.ACTIVE,
            ok=True,
            supported=True,
            checked_at=datetime.now(UTC),
            detail=f"Open Prices API {OP_API_BASE}",
        )

    # -- store resolution & discovery ---------------------------------------- #
    def resolve_store(
        self,
        *,
        postal_code: str | None = None,
        store_id: str | None = None,
        external_store_id: str | None = None,
    ) -> StoreResolutionResult:
        """Resolve an ``osm:{TYPE}/{id}`` external code (or the configured location) to a store."""
        osm = parse_osm_from_external_code(external_store_id) if external_store_id else None
        if osm is None and self._osm_id is not None and self._osm_type is not None:
            osm = (self._osm_id, self._osm_type)
        if osm is None:
            return StoreResolutionResult.unsupported("no OSM store location to resolve")
        osm_id, osm_type = osm
        ref = store_external_code(osm_type, osm_id)
        return StoreResolutionResult(
            ok=True,
            supported=True,
            resolved_retailer_code=self.retailer_code,
            resolved_store_ref=ref,
            external_store_id=ref,
            scope=PriceScope.EXACT_STORE,
            resolution_method="osm_location",
            confidence=Decimal("1.0"),
            evidence={"osm_id": osm_id, "osm_type": osm_type},
        )

    def discover_products(self, *, cursor: str | None = None) -> FetchResult:
        """Enumerate the barcodes Open Prices holds a price for at this store (one API pull)."""
        if not self._enabled or self._osm_id is None or self._osm_type is None:
            return FetchResult(ok=True, supported=True, status_code=200, payload=())
        self._ensure_loaded()
        return FetchResult(
            ok=True,
            supported=True,
            status_code=200,
            payload=tuple(sorted(self._by_barcode)),
        )

    # -- fetch --------------------------------------------------------------- #
    def fetch_product(self, external_id: str, **kwargs: object) -> FetchResult:
        """Return the raw Open Prices record (latest price at this store) for a barcode."""
        if not self._enabled:
            return FetchResult(ok=False, supported=True, error="open_prices disabled")
        self._ensure_loaded()
        price = self._by_barcode.get(external_id)
        if price is None:
            return FetchResult(
                ok=False,
                supported=True,
                status_code=404,
                error=f"no open price for barcode {external_id!r}",
            )
        raw = self._raw_payload(price)
        body = json.dumps(raw, sort_keys=True, default=str).encode("utf-8")
        return FetchResult(
            ok=True,
            supported=True,
            url=price.source_url,
            status_code=200,
            content=body,
            content_type="application/json",
            body_hash=hashlib.sha256(body).hexdigest(),
            payload=raw,
        )

    def _ensure_loaded(self) -> None:
        """Pull the store's prices once (via the adapter) and keep the latest per barcode.

        The adapter degrades gracefully (partial result, never a crash); this is wrapped
        defensively so an unexpected error still yields an empty, honest result.
        """
        if self._loaded:
            return
        self._loaded = True
        if self._osm_id is None or self._osm_type is None:
            return
        try:
            prices = self._adapter.fetch_store_prices(self._osm_id, self._osm_type)
        except Exception as exc:  # defensive: never let a source error crash the pipeline
            logger.warning("Open Prices fetch failed for %s/%s: %s",
                           self._osm_type, self._osm_id, exc)
            return
        latest: dict[str, OpenPrice] = {}
        for price in prices:
            if not price.barcode:
                continue  # loose/category rows have no barcode — never fabricate one
            current = latest.get(price.barcode)
            if current is None or (price.observed_on, price.price_id) > (
                current.observed_on,
                current.price_id,
            ):
                latest[price.barcode] = price
        self._by_barcode = latest

    def _raw_payload(self, price: OpenPrice) -> dict[str, object]:
        """The "raw" record for one barcode's latest Open Prices observation at this store."""
        pkg_qty, pkg_unit = _package_from_price_per(price.price_per)
        package = (
            {"quantity": pkg_qty, "unit": pkg_unit, "count": 1}
            if pkg_unit is not None
            else None
        )
        return {
            "external_id": price.barcode,
            "barcode": price.barcode,
            "name": price.product_name or f"Producto {price.barcode}",
            "brand": None,
            "package": package,
            "price": {
                "amount": str(price.amount),
                "currency": price.currency,
                "type": PriceType.REGULAR.value,
            },
            "observed_on": price.observed_on.isoformat(),
            "source_url": price.source_url,
            "osm_id": price.location_osm_id if price.location_osm_id is not None else self._osm_id,
            "osm_type": price.location_osm_type or self._osm_type,
        }

    # -- parse & normalize --------------------------------------------------- #
    def parse_product(self, capture: object, **kwargs: object) -> ParseResult:
        """Parse a raw Open Prices capture into records, then normalize them to observations."""
        raw = capture.payload if isinstance(capture, FetchResult) else capture
        records = self._parse_raw(raw)
        if not records:
            return ParseResult(ok=False, supported=True, error="no parseable records")
        return self.normalize_product(records)

    def _parse_raw(self, raw: object) -> list[OpenPricesParsedRecord]:
        rows = raw if isinstance(raw, (list, tuple)) else [raw]
        records: list[OpenPricesParsedRecord] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            barcode = _clean(row.get("barcode")) or _clean(row.get("external_id"))
            if not barcode:
                continue  # a barcode-less row is never turned into a price
            price = row.get("price")
            if not isinstance(price, dict):
                continue
            observed_on = _parse_iso_date(row.get("observed_on"))
            if observed_on is None:
                continue
            package = row.get("package") if isinstance(row.get("package"), dict) else None
            records.append(
                OpenPricesParsedRecord(
                    barcode=barcode,
                    name=str(row.get("name") or barcode),
                    amount=str(price.get("amount")),
                    currency=str(price.get("currency") or "EUR"),
                    observed_on=observed_on,
                    source_url=_clean(row.get("source_url")),
                    package_quantity=(
                        str(package["quantity"])
                        if package and package.get("quantity") is not None
                        else None
                    ),
                    package_unit=(
                        str(package["unit"]) if package and package.get("unit") else None
                    ),
                )
            )
        return records

    def normalize_product(self, parsed: object, **kwargs: object) -> ParseResult:
        """Normalize parsed Open Prices records into :class:`NormalizedObservation`s.

        Reuses the shared :class:`PriceNormalizer` (Decimal money, coherent ``€/kg`` · ``€/unit``
        unit prices). ``observed_at`` is the real price date; scope is ``exact_store`` (an OSM
        location); ``price_type`` is always ``regular`` and no promotion is ever attached. A row
        the normalizer rejects (e.g. an unsupported currency) is skipped with a warning, never
        fabricated.
        """
        records = self._as_records(parsed)
        observations: list[NormalizedObservation] = []
        warnings: list[str] = []
        for rec in records:
            try:
                price = self._price_normalizer.normalize(
                    rec.amount,
                    rec.currency,
                    package_quantity=rec.package_quantity,
                    package_unit=rec.package_unit,
                    package_count=1,
                )
            except NormalizationError as exc:
                warnings.append(f"{rec.barcode}: {exc}")
                continue
            if price.amount is None:
                warnings.append(f"{rec.barcode}: missing price amount")
                continue
            observed_at = datetime.combine(rec.observed_on, time.min, tzinfo=UTC)
            observations.append(
                NormalizedObservation(
                    variant_ref=rec.barcode,
                    amount=price.amount,
                    currency=price.currency,
                    price_scope=PriceScope.EXACT_STORE,
                    price_type=PriceType.REGULAR,
                    observed_at=observed_at,
                    unit_amount=price.unit_amount,
                    unit_code=price.unit_code,
                    promotion=None,
                    requires_loyalty=False,
                    available=None,
                    confidence=_OP_CONFIDENCE,
                    source=SourceRef(
                        source_slug=OP_DATA_SOURCE_SLUG,
                        source_url=rec.source_url,
                        connector_version=self.connector_version,
                        parser_version=self.parser_version,
                    ),
                )
            )
        return ParseResult(
            ok=True, supported=True, observations=tuple(observations), warnings=tuple(warnings)
        )

    def _as_records(self, parsed: object) -> list[OpenPricesParsedRecord]:
        if isinstance(parsed, OpenPricesParsedRecord):
            return [parsed]
        if isinstance(parsed, ParseResult):  # already normalized: nothing to redo
            return []
        if isinstance(parsed, (list, tuple)):
            out: list[OpenPricesParsedRecord] = []
            for item in parsed:
                if isinstance(item, OpenPricesParsedRecord):
                    out.append(item)
                elif isinstance(item, dict):
                    out.extend(self._parse_raw(item))
            return out
        if isinstance(parsed, dict):
            return self._parse_raw(parsed)
        return []

    # -- validate ------------------------------------------------------------ #
    def validate_observation(self, observation: NormalizedObservation) -> ValidationResult:
        """Validate an observation: every Open Prices price has a real (OSM) store link."""
        return self._validator.validate(
            observation,
            ValidationContext(has_store_link=True, known_currencies=frozenset({"EUR"})),
        )


__all__ = [
    "OpenPricesConnector",
    "OpenPricesParsedRecord",
]
