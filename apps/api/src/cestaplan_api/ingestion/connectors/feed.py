"""CsvFeedConnector — a **second real** connector on the FASE A/B ingestion pipeline (FASE D).

Where :class:`~cestaplan_api.ingestion.connectors.openprices.OpenPricesConnector` speaks to a
paginated, crowdsourced HTTP API (Open Food Facts Open Prices, ODbL), this connector proves the
same pipeline is reusable against a **structurally different** source: a batch **price feed** —
a CSV or JSON document the operator legitimately provides (their own catalogue, a licensed feed,
tickets). No scraping, no anti-bot evasion and no fabrication: a price is only ever what a feed
row actually carries.

The feed's *shape* is the existing section-20 import contract: this connector reuses
:data:`~cestaplan_api.adapters.base.CANONICAL_COLUMNS` and the network-free
:class:`~cestaplan_api.adapters.files.CsvRetailerAdapter` /
:class:`~cestaplan_api.adapters.files.JsonRetailerAdapter` parsers to turn feed content into
canonical rows (it does **not** re-declare the column contract), and then flows each row through
the shared ingestion normalizers/validators exactly like every other connector:

    discover -> fetch -> parse -> normalize -> validate -> record (append-only) -> coverage ->
    project ProductPrice

Adding it required **no** change to the pipeline, queue, worker, history, anomaly or coverage
code — only this new :class:`RetailerConnector` plus a registry line (see FASE D doc).

Honesty invariants:
- ``full_catalog=False`` / ``partial_catalog=True`` — a feed is whatever the operator supplies.
- Capabilities are computed from the *actual* feed content: ``promotions`` only if the feed
  carries a promo column, ``barcodes`` only if a barcode column is present, ``exact_store_scope``
  only for rows that carry a store (store-less rows are ``national`` — never ``exact_store``).
- Promotions (``2x1`` etc.) are parsed to a structured rule, never collapsed into a price.
- A row without a usable price is skipped (missing is never turned into ``0``); a store-less row
  never claims an ``exact_store`` scope.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit

from cestaplan_api.adapters.base import RawRow
from cestaplan_api.adapters.files import CsvRetailerAdapter, JsonRetailerAdapter
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
    PromotionInfo,
    RetailerConnector,
    SourcePolicy,
    SourceRef,
    StoreResolutionResult,
    ValidationResult,
)
from cestaplan_api.ingestion.http_fetcher import HttpFetcher
from cestaplan_api.ingestion.normalization import (
    NormalizationError,
    PriceNormalizer,
    PromotionParser,
)
from cestaplan_api.ingestion.validation import ObservationValidator, ValidationContext

logger = logging.getLogger(__name__)

#: Confidence carried by an operator-provided (authorized) feed vs a manual one — mirrors the
#: importer's midpoints (authorized_partner ~0.95, manual_entry ~0.65). Never invented per row.
_CONFIDENCE_AUTHORIZED = Decimal("0.9500")
_CONFIDENCE_MANUAL = Decimal("0.6500")

#: Store-less feed rows apply nationally (a valid declared scope — never ``unknown``/``exact``).
_DEFAULT_STORELESS_SCOPE = PriceScope.NATIONAL


def _clean(value: object) -> str | None:
    """Trim a raw string to ``None`` when empty."""
    return value.strip() if isinstance(value, str) and value.strip() else None


def _parse_dt(value: object) -> datetime | None:
    """Parse an ISO-8601 ``observed_at`` to an aware UTC datetime (``None`` if unparseable)."""
    text = value.strip() if isinstance(value, str) else ""
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


@dataclass(frozen=True, slots=True)
class FeedParsedRecord:
    """A parsed feed row: raw canonical fields extracted from a capture, not yet normalized."""

    external_id: str
    name: str
    amount: str | None
    currency: str
    observed_at: datetime
    package_quantity: str | None
    package_unit: str | None
    package_count: int
    brand: str | None
    barcode: str | None
    store_external_code: str | None
    promotion_text: str | None
    canonical_name: str | None
    source_url: str | None


class CsvFeedConnector(RetailerConnector):
    """A real :class:`RetailerConnector` over an operator-provided CSV/JSON price feed.

    The feed source is whatever the operator hands the connector: raw ``feed`` content
    (``str``/``bytes``), a ``feed_path`` file, or a ``feed_url`` fetched via an injected
    :class:`~cestaplan_api.ingestion.http_fetcher.HttpFetcher` (only that path touches the
    network). The feed is parsed once (lazily) with the shared file adapters, so the column
    contract is reused, never duplicated.

    Capabilities are derived from the actual feed content and are therefore honest per feed.
    Gated by ``enabled`` (the caller passes the feed's ``DataSource.is_enabled`` state); a
    disabled connector discovers nothing and reports ``DISABLED``.
    """

    retailer_code = "csv_feed"
    connector_version = "1.0.0"
    parser_version = "1.0.0"

    def __init__(
        self,
        *,
        feed: str | bytes | None = None,
        feed_path: str | Path | None = None,
        feed_url: str | None = None,
        feed_format: str = "csv",
        mapping: dict[str, str] | None = None,
        enabled: bool = True,
        legal_status: LegalStatus = LegalStatus.AUTHORIZED,
        default_price_type: PriceType = PriceType.REGULAR,
        source_slug: str = "operator-feed",
        contact: str | None = None,
        fetcher: HttpFetcher | None = None,
    ) -> None:
        if legal_status is LegalStatus.PROHIBITED:
            raise ValueError("a PROHIBITED source must never be ingested")
        if default_price_type not in (PriceType.REGULAR, PriceType.MANUAL):
            raise ValueError("feed default_price_type must be REGULAR or MANUAL")
        self._feed = feed
        self._feed_path = Path(feed_path) if feed_path is not None else None
        self._feed_url = feed_url
        self._feed_format = feed_format.lower()
        self._mapping = mapping
        self._enabled = enabled
        self._legal_status = legal_status
        self._default_price_type = default_price_type
        self._source_slug = source_slug
        self._contact = contact
        self._fetcher = fetcher

        self._price_normalizer = PriceNormalizer()
        self._promotion_parser = PromotionParser()
        self._validator = ObservationValidator()

        self._loaded = False
        self._parse_ok = False
        self._rows: dict[str, RawRow] = {}
        # Capability flags computed from the actual feed content (all honest).
        self._has_store = False
        self._has_storeless = False
        self._has_promotions = False
        self._has_loyalty = False
        self._has_barcodes = False

    # -- required ------------------------------------------------------------ #
    def capabilities(self) -> Capabilities:
        """Prices from a partial (operator-supplied) catalogue; the rest reflects the feed."""
        self._ensure_loaded()
        return Capabilities(
            prices=True,
            partial_catalog=True,
            full_catalog=False,  # a feed is whatever the operator supplies — honest.
            promotions=self._has_promotions,
            loyalty_prices=self._has_loyalty,
            barcodes=self._has_barcodes,
            exact_store_scope=self._has_store,
            national_scope=self._has_storeless,
            # Honest about everything a plain price feed does NOT provide:
            availability=False,
            delivery_zone_scope=False,
            regional_scope=False,
            product_images=False,
            nutrition=False,
            incremental_sync=False,
        )

    def source_policy(self) -> SourcePolicy:
        """Operator-provided/licensed feed: AUTHORIZED (or MANUAL); robots N/A for a file."""
        host = urlsplit(self._feed_url).hostname if self._feed_url else None
        return SourcePolicy(
            allowed_domains=(host,) if host else (),
            request_delay=0.0 if self._feed_url is None else 1.0,
            max_concurrency=1,
            respects_robots=True,
            legal_status=self._legal_status,
            contact=self._contact,
        )

    # -- health -------------------------------------------------------------- #
    def health_check(self) -> HealthResult:
        """Report whether the feed is enabled and parseable (no fabricated probe)."""
        if not self._enabled:
            return HealthResult(
                status=ConnectorStatus.DISABLED,
                ok=False,
                supported=True,
                checked_at=datetime.now(UTC),
                detail="feed source disabled",
            )
        self._ensure_loaded()
        if not self._parse_ok:
            return HealthResult(
                status=ConnectorStatus.SOURCE_UNAVAILABLE,
                ok=False,
                supported=True,
                checked_at=datetime.now(UTC),
                detail="feed is unreachable or unparseable",
            )
        return HealthResult(
            status=ConnectorStatus.ACTIVE,
            ok=True,
            supported=True,
            checked_at=datetime.now(UTC),
            detail=f"feed parsed ({len(self._rows)} rows, {self._feed_format})",
        )

    # -- store resolution & discovery ---------------------------------------- #
    def resolve_store(
        self,
        *,
        postal_code: str | None = None,
        store_id: str | None = None,
        external_store_id: str | None = None,
    ) -> StoreResolutionResult:
        """Resolve a feed-carried ``store_external_code`` to an exact-store scope, if present."""
        code = _clean(external_store_id)
        if code is None:
            return StoreResolutionResult.unsupported("no store code supplied to resolve")
        return StoreResolutionResult(
            ok=True,
            supported=True,
            resolved_retailer_code=self.retailer_code,
            resolved_store_ref=code,
            external_store_id=code,
            scope=PriceScope.EXACT_STORE,
            resolution_method="feed_store_code",
            confidence=Decimal("1.0"),
            evidence={"store_external_code": code},
        )

    def discover_products(self, *, cursor: str | None = None) -> FetchResult:
        """Enumerate the external ids the feed carries a row for (one parse of the feed)."""
        if not self._enabled:
            return FetchResult(ok=True, supported=True, status_code=200, payload=())
        self._ensure_loaded()
        return FetchResult(
            ok=True,
            supported=True,
            status_code=200,
            payload=tuple(sorted(self._rows)),
        )

    # -- fetch --------------------------------------------------------------- #
    def fetch_product(self, external_id: str, **kwargs: object) -> FetchResult:
        """Return the raw feed record for one external id (already in memory, no I/O)."""
        if not self._enabled:
            return FetchResult(ok=False, supported=True, error="feed disabled")
        self._ensure_loaded()
        row = self._rows.get(external_id)
        if row is None:
            return FetchResult(
                ok=False,
                supported=True,
                status_code=404,
                error=f"no feed row for external_id {external_id!r}",
            )
        raw = self._raw_payload(external_id, row)
        return FetchResult(
            ok=True,
            supported=True,
            url=raw.get("source_url"),  # type: ignore[arg-type]
            status_code=200,
            content_type="text/csv" if self._feed_format == "csv" else "application/json",
            payload=raw,
        )

    def _raw_payload(self, external_id: str, row: RawRow) -> dict[str, object]:
        """The "raw" record for one feed row, keyed for pipeline variant resolution."""
        qty = _clean(row.get("package_quantity"))
        unit = _clean(row.get("package_unit"))
        package = (
            {"quantity": qty, "unit": unit, "count": 1}
            if qty is not None or unit is not None
            else None
        )
        return {
            "external_id": external_id,
            "barcode": _clean(row.get("barcode")),
            "name": _clean(row.get("product_name")) or external_id,
            "brand": _clean(row.get("brand")),
            "package": package,
            "price": {
                "amount": _clean(row.get("amount")),
                "currency": _clean(row.get("currency")) or "EUR",
            },
            "observed_at": _clean(row.get("observed_at")),
            "store_external_code": _clean(row.get("store_external_code")),
            "promotion": _clean(row.get("promotion")),
            "canonical_name": _clean(row.get("canonical_name")),
            "source_url": _clean(row.get("source_url")),
        }

    # -- parse & normalize --------------------------------------------------- #
    def parse_product(self, capture: object, **kwargs: object) -> ParseResult:
        """Parse a raw feed capture into records, then normalize them to observations."""
        raw = capture.payload if isinstance(capture, FetchResult) else capture
        records = self._parse_raw(raw)
        if not records:
            return ParseResult(ok=False, supported=True, error="no parseable records")
        return self.normalize_product(records)

    def _parse_raw(self, raw: object) -> list[FeedParsedRecord]:
        rows = raw if isinstance(raw, (list, tuple)) else [raw]
        records: list[FeedParsedRecord] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            external_id = _clean(row.get("external_id")) or _clean(row.get("barcode"))
            if external_id is None:
                continue  # a row with no stable id is never turned into a price
            observed_at = _parse_dt(row.get("observed_at"))
            if observed_at is None:
                continue  # no observation date -> not a trustworthy price row
            price = row.get("price") if isinstance(row.get("price"), dict) else {}
            package = row.get("package") if isinstance(row.get("package"), dict) else {}
            records.append(
                FeedParsedRecord(
                    external_id=external_id,
                    name=str(row.get("name") or external_id),
                    amount=_clean(price.get("amount")) if isinstance(price, dict) else None,
                    currency=(
                        _clean(price.get("currency")) or "EUR"
                        if isinstance(price, dict)
                        else "EUR"
                    ),
                    observed_at=observed_at,
                    package_quantity=(
                        _clean(package.get("quantity")) if isinstance(package, dict) else None
                    ),
                    package_unit=(
                        _clean(package.get("unit")) if isinstance(package, dict) else None
                    ),
                    package_count=1,
                    brand=_clean(row.get("brand")),
                    barcode=_clean(row.get("barcode")),
                    store_external_code=_clean(row.get("store_external_code")),
                    promotion_text=_clean(row.get("promotion")),
                    canonical_name=_clean(row.get("canonical_name")),
                    source_url=_clean(row.get("source_url")),
                )
            )
        return records

    def normalize_product(self, parsed: object, **kwargs: object) -> ParseResult:
        """Normalize parsed feed records into :class:`NormalizedObservation`s.

        Reuses the shared :class:`PriceNormalizer` (Decimal money, coherent ``€/kg`` · ``€/l`` ·
        ``€/unit``) and :class:`PromotionParser` (``2x1`` etc. modelled, never collapsed). Scope
        is ``exact_store`` only for a row carrying a store, else ``national``. A row with no
        usable price is skipped with a warning — a missing price is never turned into ``0``.
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
                    package_count=rec.package_count,
                )
            except NormalizationError as exc:
                warnings.append(f"{rec.external_id}: {exc}")
                continue
            if price.amount is None:
                warnings.append(f"{rec.external_id}: missing price amount")
                continue

            promotion = self._promotion_parser.parse(rec.promotion_text)
            price_type, requires_loyalty = self._price_type(promotion)
            scope = (
                PriceScope.EXACT_STORE
                if rec.store_external_code is not None
                else _DEFAULT_STORELESS_SCOPE
            )
            observations.append(
                NormalizedObservation(
                    variant_ref=rec.external_id,
                    amount=price.amount,
                    currency=price.currency,
                    price_scope=scope,
                    price_type=price_type,
                    observed_at=rec.observed_at,
                    unit_amount=price.unit_amount,
                    unit_code=price.unit_code,
                    promotion=promotion,
                    requires_loyalty=requires_loyalty,
                    available=None,
                    confidence=self._confidence(),
                    source=SourceRef(
                        source_slug=self._source_slug,
                        source_url=rec.source_url,
                        connector_version=self.connector_version,
                        parser_version=self.parser_version,
                    ),
                )
            )
        return ParseResult(
            ok=True, supported=True, observations=tuple(observations), warnings=tuple(warnings)
        )

    def _price_type(self, promotion: PromotionInfo | None) -> tuple[PriceType, bool]:
        """Row price type: the feed default (``regular``/``manual``) unless a promo overrides."""
        if promotion is None:
            return self._default_price_type, False
        if promotion.loyalty_required:
            return PriceType.LOYALTY, True
        return PriceType.PROMOTIONAL, False

    def _confidence(self) -> Decimal:
        """Confidence by legal footing: AUTHORIZED feeds are trusted more than others."""
        return (
            _CONFIDENCE_AUTHORIZED
            if self._legal_status is LegalStatus.AUTHORIZED
            else _CONFIDENCE_MANUAL
        )

    def _as_records(self, parsed: object) -> list[FeedParsedRecord]:
        if isinstance(parsed, FeedParsedRecord):
            return [parsed]
        if isinstance(parsed, ParseResult):  # already normalized: nothing to redo
            return []
        if isinstance(parsed, (list, tuple)):
            out: list[FeedParsedRecord] = []
            for item in parsed:
                if isinstance(item, FeedParsedRecord):
                    out.append(item)
                elif isinstance(item, dict):
                    out.extend(self._parse_raw(item))
            return out
        if isinstance(parsed, dict):
            return self._parse_raw(parsed)
        return []

    # -- validate ------------------------------------------------------------ #
    def validate_observation(self, observation: NormalizedObservation) -> ValidationResult:
        """Validate an observation; ``exact_store`` is only claimed with a feed store link."""
        has_store_link = observation.price_scope is PriceScope.EXACT_STORE
        return self._validator.validate(
            observation,
            ValidationContext(
                has_store_link=has_store_link, known_currencies=frozenset({"EUR"})
            ),
        )

    # -- feed loading -------------------------------------------------------- #
    def _adapter(self) -> CsvRetailerAdapter | JsonRetailerAdapter:
        if self._feed_format == "json":
            return JsonRetailerAdapter()
        return CsvRetailerAdapter()

    def _load_content(self) -> str | bytes | None:
        """Obtain the raw feed content from the given source (string/path/URL)."""
        if self._feed is not None:
            return self._feed
        if self._feed_path is not None:
            try:
                return self._feed_path.read_text(encoding="utf-8")
            except OSError as exc:  # defensive: unreadable file -> honest empty result
                logger.warning("feed file %s unreadable: %s", self._feed_path, exc)
                return None
        if self._feed_url is not None and self._fetcher is not None:
            result = self._fetcher.fetch(self._feed_url, policy=self.source_policy())
            if not result.ok or result.content is None:
                logger.warning("feed url %s fetch failed: %s", self._feed_url, result.error)
                return None
            return result.content
        return None

    def _ensure_loaded(self) -> None:
        """Parse the feed once (via the shared file adapters) and index rows by external id.

        Degrades gracefully: a missing/unreadable/unparseable feed yields an empty, honest
        result (``_parse_ok=False``) rather than raising.
        """
        if self._loaded:
            return
        self._loaded = True
        if not self._enabled:
            return
        content = self._load_content()
        if content is None:
            return
        parsed = self._adapter().parse(content, self._mapping)
        if not parsed.rows:
            # No rows (empty or structurally broken feed): unparseable if it also errored.
            self._parse_ok = not parsed.errors
            return
        self._parse_ok = True
        for raw in parsed.rows:
            external_id = _clean(raw.get("product_external_id")) or _clean(raw.get("barcode"))
            if external_id is None:
                continue  # never fabricate an identifier for an unkeyed row
            self._rows[external_id] = raw
            self._update_flags(raw)

    def _update_flags(self, raw: RawRow) -> None:
        """Fold one row into the honest capability flags computed from the feed."""
        if _clean(raw.get("store_external_code")) is not None:
            self._has_store = True
        else:
            self._has_storeless = True
        if _clean(raw.get("barcode")) is not None:
            self._has_barcodes = True
        promo = _clean(raw.get("promotion"))
        if promo is not None:
            self._has_promotions = True
            parsed = self._promotion_parser.parse(promo)
            if parsed is not None and parsed.loyalty_required:
                self._has_loyalty = True


__all__ = [
    "CsvFeedConnector",
    "FeedParsedRecord",
]
