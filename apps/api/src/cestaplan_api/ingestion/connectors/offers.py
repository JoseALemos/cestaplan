"""OFFER connectors (Lidl, Aldi, Deza) for the price-ingestion subsystem (FASE E).

These are **PARTIAL / OFFER** sources, not full catalogues, and they are **honest and legal**:

- No live scraping is ever performed. A weekly-leaflet *offers* endpoint for these chains has
  **no authorized public source** (their data endpoints are ``robots.txt``-disallowed for
  Lidl/Aldi, and Deza has no supported data source at all — its real path is an admin import).
  So every live path (:meth:`health_check`, :meth:`fetch_product`, :meth:`fetch_offers`) returns
  a *controlled* result reflecting the source's footing — ``permission_required`` for Lidl/Aldi,
  ``unsupported`` for Deza — and **never** issues an HTTP request.
- The connectors are **disabled by default** (gated by the ``LIDL_OFFERS_CONNECTOR_ENABLED`` /
  ``ALDI_OFFERS_CONNECTOR_ENABLED`` / ``DEZA_CONNECTOR_ENABLED`` flags via the registry).
- The only path that yields observations is a **synthetic offers fixture** (a Python dict in
  the weekly-leaflet shape — NO real HTML/PDF), used by tests and, in production, only when an
  operator supplies an *authorized* offers feed. Even then it is honest: an offer is a
  *promotion*, so ``capabilities().full_catalog`` is ``False``, ``promotions`` is ``True`` and
  ``prices`` (a full regular-price catalogue) is ``False`` — an offer source is **never**
  presented as covering the whole supermarket.

Each observation is ``price_type=promotional`` (or ``loyalty`` when the leaflet flags a
loyalty-card price) and carries a structured :class:`PromotionInfo` with the promotion's
**validity dates** — the promo is modelled, never collapsed into a bare unit price.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime, time
from decimal import Decimal

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
    PromotionType,
    RetailerConnector,
    SourcePolicy,
    SourceRef,
    ValidationResult,
)
from cestaplan_api.ingestion.normalization import (
    NormalizationError,
    PriceNormalizer,
    PromotionParser,
)
from cestaplan_api.ingestion.validation import ObservationValidator, ValidationContext

#: Confidence carried by an operator-authorized offers feed (leaflet promo prices).
_OFFERS_CONFIDENCE = Decimal("0.7500")

#: Offers from a weekly leaflet apply nationally (a valid declared scope — never exact_store).
_OFFERS_SCOPE = PriceScope.NATIONAL


def _clean(value: object) -> str | None:
    """Trim a raw string to ``None`` when empty."""
    return value.strip() if isinstance(value, str) and value.strip() else None


def _parse_date(value: object) -> datetime | None:
    """Parse an ISO date/datetime to an aware UTC datetime (``None`` if unparseable)."""
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    text = value.strip() if isinstance(value, str) else ""
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        try:
            return datetime.combine(datetime.fromisoformat(text[:10]).date(), time.min, tzinfo=UTC)
        except ValueError:
            return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


@dataclass(frozen=True, slots=True)
class OfferParsedRecord:
    """A parsed weekly-leaflet offer row: raw fields extracted from a capture, pre-normalize."""

    external_id: str
    name: str
    brand: str | None
    package_quantity: str | None
    package_unit: str | None
    package_count: int
    amount: str | None
    currency: str
    promotion_text: str | None
    valid_from: datetime | None
    valid_until: datetime | None
    loyalty: bool
    source_url: str | None


class _OffersConnector(RetailerConnector):
    """Base for an OFFERS connector: honest, disabled by default, no live scraping.

    A subclass sets its identity (:attr:`retailer_code`, :attr:`_source_slug`) and its legal
    footing (:attr:`_legal_status` for the :class:`SourcePolicy`, :attr:`_live_status` for the
    controlled result every live path returns). ``offers`` is a synthetic fixture in the
    weekly-leaflet shape (only supplied by tests / an authorized operator feed); absent it, the
    connector discovers nothing and every live path yields the controlled footing result.
    """

    connector_version = "1.0.0"
    parser_version = "1.0.0"

    #: Legal footing surfaced by :meth:`source_policy` (blocks enabling in the admin router).
    _legal_status: LegalStatus = LegalStatus.PERMISSION_REQUIRED
    #: Connector status every live path reports (``PERMISSION_REQUIRED`` or ``UNSUPPORTED``).
    _live_status: ConnectorStatus = ConnectorStatus.PERMISSION_REQUIRED
    #: Stable source slug recorded on observations from an authorized offers feed.
    _source_slug: str = "offers"
    #: Human-readable reason returned by the controlled live result.
    _live_reason: str = "no authorized public offers source; live fetch not performed"

    def __init__(
        self,
        *,
        offers: list[dict[str, object]] | tuple[dict[str, object], ...] | None = None,
        enabled: bool = False,
        contact: str | None = None,
    ) -> None:
        self._enabled = enabled
        self._contact = contact
        self._offers = self._index(offers)
        self._price_normalizer = PriceNormalizer()
        self._promotion_parser = PromotionParser()
        self._validator = ObservationValidator()

    @staticmethod
    def _index(
        offers: list[dict[str, object]] | tuple[dict[str, object], ...] | None,
    ) -> dict[str, dict[str, object]]:
        indexed: dict[str, dict[str, object]] = {}
        for offer in offers or ():
            if not isinstance(offer, dict):
                continue
            external_id = _clean(offer.get("external_id"))
            if external_id is None:
                continue  # never fabricate an identifier for an unkeyed offer row
            indexed[external_id] = offer
        return indexed

    @property
    def _live_supported(self) -> bool:
        """``permission_required`` is supported-but-not-permitted; ``unsupported`` is not."""
        return self._live_status is not ConnectorStatus.UNSUPPORTED

    # -- required ------------------------------------------------------------ #
    def capabilities(self) -> Capabilities:
        """Honest OFFERS capabilities: partial, promotions-only — never a full catalogue."""
        return Capabilities(
            full_catalog=False,  # an offer leaflet is NOT the whole supermarket — honest.
            partial_catalog=True,
            prices=False,  # offers carry promo prices, not a full regular-price catalogue.
            promotions=True,
            loyalty_prices=True,  # leaflets may flag a loyalty-card price (Dia-style).
            national_scope=True,
            # Honest about everything an offers leaflet does NOT provide:
            availability=False,
            exact_store_scope=False,
            delivery_zone_scope=False,
            regional_scope=False,
            product_images=False,
            barcodes=False,
            nutrition=False,
            incremental_sync=False,
        )

    def source_policy(self) -> SourcePolicy:
        """The source's access policy: robots-respecting, with its true legal footing."""
        return SourcePolicy(
            allowed_domains=(),
            request_delay=1.0,
            max_concurrency=1,
            respects_robots=True,
            legal_status=self._legal_status,
            contact=self._contact,
        )

    # -- health -------------------------------------------------------------- #
    def health_check(self) -> HealthResult:
        """Report state without ever touching the network.

        Disabled -> ``DISABLED``. Enabled with an authorized offers fixture -> ``ACTIVE``.
        Enabled with no fixture (the live path) -> the controlled footing result
        (``permission_required`` / ``unsupported``); no HTTP request is made.
        """
        now = datetime.now(UTC)
        if not self._enabled:
            return HealthResult(
                status=ConnectorStatus.DISABLED,
                ok=False,
                supported=True,
                checked_at=now,
                detail="offers connector disabled",
            )
        if self._offers:
            return HealthResult(
                status=ConnectorStatus.ACTIVE,
                ok=True,
                supported=True,
                checked_at=now,
                detail=f"authorized offers feed parsed ({len(self._offers)} offers)",
            )
        return HealthResult(
            status=self._live_status,
            ok=False,
            supported=self._live_supported,
            checked_at=now,
            detail=self._live_reason,
        )

    # -- discovery ----------------------------------------------------------- #
    def discover_products(self, *, cursor: str | None = None) -> FetchResult:
        """Enumerate the offer ids from an authorized fixture; disabled/live discovers nothing."""
        if not self._enabled or not self._offers:
            return FetchResult(ok=True, supported=True, status_code=200, payload=())
        return FetchResult(
            ok=True, supported=True, status_code=200, payload=tuple(sorted(self._offers))
        )

    # -- fetch --------------------------------------------------------------- #
    def fetch_product(self, external_id: str, **kwargs: object) -> FetchResult:
        """Return one offer's raw record from the fixture, or the controlled live result."""
        if not self._enabled:
            return FetchResult(ok=False, supported=True, error="offers connector disabled")
        if not self._offers:
            return self._live_fetch_result()
        offer = self._offers.get(external_id)
        if offer is None:
            return FetchResult(
                ok=False,
                supported=True,
                status_code=404,
                error=f"no offer for external_id {external_id!r}",
            )
        raw = self._raw_payload(external_id, offer)
        body = json.dumps(raw, sort_keys=True, default=str).encode("utf-8")
        return FetchResult(
            ok=True,
            supported=True,
            url=raw.get("source_url"),  # type: ignore[arg-type]
            status_code=200,
            content=body,
            content_type="application/json",
            payload=raw,
        )

    def fetch_offers(self, **kwargs: object) -> FetchResult:
        """Return every fixture offer's raw record, or the controlled live result."""
        if not self._enabled:
            return FetchResult(ok=False, supported=True, error="offers connector disabled")
        if not self._offers:
            return self._live_fetch_result()
        rows = tuple(
            self._raw_payload(external_id, offer)
            for external_id, offer in sorted(self._offers.items())
        )
        return FetchResult(ok=True, supported=True, status_code=200, payload=rows)

    def _live_fetch_result(self) -> FetchResult:
        """A controlled result reflecting the source's footing — NEVER a live HTTP request."""
        if self._live_supported:
            # permission_required: the operation is understood but not permitted without consent.
            return FetchResult(ok=False, supported=True, error=self._live_reason)
        # unsupported (Deza scraping): the real path is an admin import, not this connector.
        return FetchResult.unsupported(self._live_reason)

    def _raw_payload(self, external_id: str, offer: dict[str, object]) -> dict[str, object]:
        """The "raw" record for one leaflet offer (promo price + validity + loyalty flag)."""
        package = offer.get("package") if isinstance(offer.get("package"), dict) else None
        promo = offer.get("promo_price") if isinstance(offer.get("promo_price"), dict) else {}
        currency = (
            _clean(promo.get("currency")) if isinstance(promo, dict) else None
        ) or _clean(offer.get("currency")) or "EUR"
        amount = _clean(promo.get("amount")) if isinstance(promo, dict) else None
        return {
            "external_id": external_id,
            "name": _clean(offer.get("name")) or external_id,
            "brand": _clean(offer.get("brand")),
            "package": package,
            "promo_price": {"amount": amount, "currency": currency},
            "regular_price": offer.get("regular_price"),
            "promotion": _clean(offer.get("promotion")),
            "valid_from": offer.get("valid_from"),
            "valid_until": offer.get("valid_until"),
            "loyalty": bool(offer.get("loyalty", False)),
            "source_url": _clean(offer.get("source_url")),
        }

    # -- parse & normalize --------------------------------------------------- #
    def parse_product(self, capture: object, **kwargs: object) -> ParseResult:
        """Parse a raw offer capture into records, then normalize them to observations."""
        raw = capture.payload if isinstance(capture, FetchResult) else capture
        records = self._parse_raw(raw)
        if not records:
            return ParseResult(ok=False, supported=True, error="no parseable offer records")
        return self.normalize_product(records)

    def _parse_raw(self, raw: object) -> list[OfferParsedRecord]:
        rows = raw if isinstance(raw, (list, tuple)) else [raw]
        records: list[OfferParsedRecord] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            external_id = _clean(row.get("external_id"))
            if external_id is None:
                continue  # an offer with no stable id is never turned into a price
            promo = row.get("promo_price") if isinstance(row.get("promo_price"), dict) else {}
            package = row.get("package") if isinstance(row.get("package"), dict) else {}
            records.append(
                OfferParsedRecord(
                    external_id=external_id,
                    name=str(row.get("name") or external_id),
                    brand=_clean(row.get("brand")),
                    package_quantity=(
                        _clean(package.get("quantity")) if isinstance(package, dict) else None
                    ),
                    package_unit=(
                        _clean(package.get("unit")) if isinstance(package, dict) else None
                    ),
                    package_count=(
                        int(package["count"])
                        if isinstance(package, dict)
                        and str(package.get("count") or "").isdigit()
                        else 1
                    ),
                    amount=_clean(promo.get("amount")) if isinstance(promo, dict) else None,
                    currency=(
                        _clean(promo.get("currency")) or "EUR"
                        if isinstance(promo, dict)
                        else "EUR"
                    ),
                    promotion_text=_clean(row.get("promotion")),
                    valid_from=_parse_date(row.get("valid_from")),
                    valid_until=_parse_date(row.get("valid_until")),
                    loyalty=bool(row.get("loyalty", False)),
                    source_url=_clean(row.get("source_url")),
                )
            )
        return records

    def normalize_product(self, parsed: object, **kwargs: object) -> ParseResult:
        """Normalize parsed offer records into promotional :class:`NormalizedObservation`s.

        The recorded ``amount`` is the real leaflet promo price (never collapsed from the promo
        rule); ``price_type`` is ``promotional`` (or ``loyalty`` when the leaflet flags a
        loyalty-card price), scope is ``national``, and a structured :class:`PromotionInfo`
        carries the promotion's **validity dates**. A row with no usable price is skipped with a
        warning — a missing price is never turned into ``0``.
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
                warnings.append(f"{rec.external_id}: missing promo price amount")
                continue

            promotion = self._promotion(rec)
            price_type = PriceType.LOYALTY if rec.loyalty else PriceType.PROMOTIONAL
            observed_at = rec.valid_from or datetime.now(UTC)
            observations.append(
                NormalizedObservation(
                    variant_ref=rec.external_id,
                    amount=price.amount,
                    currency=price.currency,
                    price_scope=_OFFERS_SCOPE,
                    price_type=price_type,
                    observed_at=observed_at,
                    unit_amount=price.unit_amount,
                    unit_code=price.unit_code,
                    promotion=promotion,
                    requires_loyalty=rec.loyalty,
                    available=None,
                    confidence=_OFFERS_CONFIDENCE,
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

    def _promotion(self, rec: OfferParsedRecord) -> PromotionInfo:
        """Build the structured promotion, stamped with the leaflet's explicit validity dates.

        The promo rule is parsed from the leaflet text (``2x1``, ``-20%``, ``2ª ud. -50%``,
        ``% con tarjeta`` …); the explicit ``valid_from``/``valid_until`` from the leaflet win
        over any dates recovered from the text, and the loyalty flag is preserved. When the text
        is unrecognised the promotion is still modelled (as a validity-carrying fixed rule) so a
        loyalty-only or bare-validity offer never loses its dates.
        """
        parsed = self._promotion_parser.parse(rec.promotion_text)
        base = parsed or PromotionInfo(
            promotion_type=PromotionType.FIXED, raw_text=rec.promotion_text
        )
        return replace(
            base,
            raw_text=rec.promotion_text if rec.promotion_text is not None else base.raw_text,
            loyalty_required=rec.loyalty or base.loyalty_required,
            valid_from=rec.valid_from if rec.valid_from is not None else base.valid_from,
            valid_until=rec.valid_until if rec.valid_until is not None else base.valid_until,
        )

    def _as_records(self, parsed: object) -> list[OfferParsedRecord]:
        if isinstance(parsed, OfferParsedRecord):
            return [parsed]
        if isinstance(parsed, ParseResult):  # already normalized: nothing to redo
            return []
        if isinstance(parsed, (list, tuple)):
            out: list[OfferParsedRecord] = []
            for item in parsed:
                if isinstance(item, OfferParsedRecord):
                    out.append(item)
                elif isinstance(item, dict):
                    out.extend(self._parse_raw(item))
            return out
        if isinstance(parsed, dict):
            return self._parse_raw(parsed)
        return []

    # -- validate ------------------------------------------------------------ #
    def validate_observation(self, observation: NormalizedObservation) -> ValidationResult:
        """Validate an observation; a national offer never claims an ``exact_store`` link."""
        has_store_link = observation.price_scope is PriceScope.EXACT_STORE
        return self._validator.validate(
            observation,
            ValidationContext(
                has_store_link=has_store_link, known_currencies=frozenset({"EUR"})
            ),
        )


class LidlOffersConnector(_OffersConnector):
    """Lidl weekly-offers connector: ``permission_required``, disabled by default, no scraping.

    Lidl's data endpoints are ``robots.txt``-disallowed (``/user-api/*``) and there is no
    authorized public offers source, so the live path returns a controlled
    ``permission_required`` result and never fetches. Observations come only from an authorized
    synthetic offers fixture (tests / an operator-supplied feed).
    """

    retailer_code = "lidl_offers"
    _legal_status = LegalStatus.PERMISSION_REQUIRED
    _live_status = ConnectorStatus.PERMISSION_REQUIRED
    _source_slug = "lidl-offers"
    _live_reason = (
        "Lidl offers: data endpoints robots-disallowed and no authorized public source; "
        "permission required — no live request performed"
    )


class AldiOffersConnector(_OffersConnector):
    """Aldi weekly-offers connector: ``permission_required``, disabled by default, no scraping.

    Aldi has no authorized public offers endpoint, so the live path returns a controlled
    ``permission_required`` result and never fetches. Observations come only from an authorized
    synthetic offers fixture (tests / an operator-supplied feed).
    """

    retailer_code = "aldi_offers"
    _legal_status = LegalStatus.PERMISSION_REQUIRED
    _live_status = ConnectorStatus.PERMISSION_REQUIRED
    _source_slug = "aldi-offers"
    _live_reason = (
        "Aldi offers: no authorized public source; permission required — "
        "no live request performed"
    )


class DezaOffersConnector(_OffersConnector):
    """Deza connector: scraping ``unsupported`` — the real path is an admin import.

    Deza is a small regional chain with no supported data source; scraping is not a supported
    path, so the live path returns a controlled ``unsupported`` result and never fetches. The
    honest way to load Deza prices is an operator admin import (or an authorized offers feed
    supplied to this connector as a fixture). Its :meth:`source_policy` footing is
    ``permission_required`` (no authorized public source), so it can never be enabled to active.
    """

    retailer_code = "deza"
    _legal_status = LegalStatus.PERMISSION_REQUIRED
    _live_status = ConnectorStatus.UNSUPPORTED
    _source_slug = "deza-offers"
    _live_reason = (
        "Deza: no supported public data source; scraping unsupported — "
        "the real path is an admin import (no live request performed)"
    )


__all__ = [
    "AldiOffersConnector",
    "DezaOffersConnector",
    "LidlOffersConnector",
    "OfferParsedRecord",
]
