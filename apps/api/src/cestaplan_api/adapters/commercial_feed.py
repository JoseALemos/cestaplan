"""Generic commercial price-feed adapter — an ``authorized_partner`` connector.

This is a **pluggable, config-driven** connector to a paid third-party price API the operator
subscribes to (RadarSuper / Pepesto / any unified grocery-price vendor). CestaPlan only ever
*consumes* an authorized API with the operator's own key — it does **not** scrape, evade
anti-bot, or fabricate data. Any provider plugs in without code changes: the base URL, auth
header, endpoint path, pagination style and a JSON field mapping all come from configuration
(``config.py`` / environment), so the adapter is provider-agnostic.

Design guarantees (mirroring ``openprices.py``):

- **Disabled by default.** With no ``COMMERCIAL_FEED_BASE_URL`` / ``COMMERCIAL_FEED_API_KEY``
  / mapping the adapter is *unconfigured* and reports ``enabled=False``; nothing runs.
- **Injectable client.** An ``httpx.Client`` (e.g. an ``httpx.MockTransport`` client in tests)
  is reused; when absent a short-lived client with a bounded timeout is opened per call.
- **Graceful.** Every failure mode (network error, timeout, non-200, malformed payload,
  unmapped/parse-less row) degrades to the records gathered so far — never a crash, never a
  fabricated price (absence stays ``None``; a row without a usable price/identity is skipped).
- **Canonical output.** :meth:`CommercialFeedAdapter.fetch_products` returns the shared
  :class:`~cestaplan_api.adapters.base.NormalizedRecord` value objects the sync service
  persists as real ``authorized_partner`` prices.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

import httpx

from cestaplan_api.adapters.base import (
    AdapterCapabilities,
    AdapterMetadata,
    AdapterStatus,
    NormalizedRecord,
    RetailerAdapter,
)

if TYPE_CHECKING:
    from cestaplan_api.config import Settings

#: Stable identity linking ``Retailer.adapter_key`` / ``DataSource.adapter_key`` / registry.
CF_ADAPTER_KEY = "commercial_feed"
#: DataSource slug for the ensured config row.
CF_DATA_SOURCE_SLUG = "commercial-feed"
#: Dominant ``source_type`` produced by this connector.
CF_SOURCE_TYPE = "authorized_partner"
#: Bounded timeout (seconds) for every read; never blocks unbounded.
CF_TIMEOUT_SECONDS = 20.0
#: Descriptive User-Agent (respectful, honest identity — no evasion).
CF_USER_AGENT = "CestaPlan/0.0 (+self-hosted; authorized-partner-feed)"
#: Safety cap on pages pulled per run (defensive against a mis-paginating provider).
CF_MAX_PAGES = 200
#: The canonical fields a provider mapping may target.
CF_CANONICAL_FIELDS: tuple[str, ...] = (
    "barcode",
    "product_ref",
    "product_name",
    "brand",
    "amount",
    "currency",
    "unit_price",
    "date",
    "store_ref",
    "category",
    "promo_price",
    "package_quantity",
    "package_unit",
)


@dataclass(slots=True)
class CommercialFeedConfig:
    """Resolved, provider-agnostic configuration for one commercial feed.

    Built from :class:`~cestaplan_api.config.Settings` via :meth:`from_settings`. ``field_map``
    maps a canonical field (see :data:`CF_CANONICAL_FIELDS`) to the provider's JSON field name
    (a dotted path is allowed for nested payloads). The connector is *configured* only when a
    base URL, an API key and a non-empty mapping are all present.
    """

    base_url: str = ""
    api_key: str = ""
    auth_header: str = "Authorization: Bearer"
    products_path: str = "/products"
    pagination: str = "none"
    page_size: int = 100
    items_path: str = ""
    field_map: dict[str, str] = field(default_factory=dict)
    source_name: str = "Feed comercial autorizado"
    attribution: str = ""
    license_code: str = "proprietary"

    @classmethod
    def from_settings(cls, settings: Settings) -> CommercialFeedConfig:
        return cls(
            base_url=settings.commercial_feed_base_url.strip(),
            api_key=settings.commercial_feed_api_key.strip(),
            auth_header=settings.commercial_feed_auth_header.strip()
            or "Authorization: Bearer",
            products_path=settings.commercial_feed_products_path.strip() or "/products",
            pagination=settings.commercial_feed_pagination,
            page_size=max(1, settings.commercial_feed_page_size),
            items_path=settings.commercial_feed_items_path.strip(),
            field_map=dict(settings.commercial_feed_field_map),
            source_name=settings.commercial_feed_source_name,
            attribution=settings.commercial_feed_attribution,
            license_code=settings.commercial_feed_license_code,
        )

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.api_key and self.field_map)

    def auth(self) -> dict[str, str]:
        """Build the auth header dict from ``"Name: Prefix"`` + the API key."""
        name, _, prefix = self.auth_header.partition(":")
        name = name.strip() or "Authorization"
        prefix = prefix.strip()
        value = f"{prefix} {self.api_key}".strip() if prefix else self.api_key
        return {name: value}


def _dig(item: dict[str, Any], path: str) -> Any:
    """Resolve a (possibly dotted) key path within a nested dict; ``None`` if absent."""
    cur: Any = item
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _clean_str(value: Any) -> str | None:
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return None


def _decimal_or_none(value: Any) -> Decimal | None:
    """Parse a numeric (number or numeric string) to Decimal; never fabricate."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str) and not value.strip():
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _parse_datetime(value: Any) -> datetime | None:
    """Parse an ISO date/datetime; ``None`` when absent/unparseable (never fabricated)."""
    text = value.strip() if isinstance(value, str) else ""
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            dt = datetime.fromisoformat(text[:10])
        except ValueError:
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


class CommercialFeedAdapter(RetailerAdapter):
    """Generic ``authorized_partner`` connector over a paid, licensed price API.

    Provider-agnostic: identity, endpoint, auth, pagination and the JSON field mapping all come
    from :class:`CommercialFeedConfig`. Its capability is a catalogue-wide price pull via
    :meth:`fetch_products`; the per-product ``RetailerAdapter`` reads stay unsupported because
    the feed is consumed as a store catalogue, not per selector. **Disabled unless configured.**
    """

    adapter_key = CF_ADAPTER_KEY
    source_type = CF_SOURCE_TYPE
    enabled = False

    def __init__(
        self,
        client: httpx.Client | None = None,
        config: CommercialFeedConfig | None = None,
    ) -> None:
        # An injected client (e.g. httpx.MockTransport in tests) is reused, not closed here;
        # when absent, each call opens and closes a short-lived, bounded-timeout client.
        self._client = client
        if config is not None:
            self._config = config
        else:
            from cestaplan_api.config import get_settings

            self._config = CommercialFeedConfig.from_settings(get_settings())

    @property
    def config(self) -> CommercialFeedConfig:
        return self._config

    def capabilities(self) -> AdapterCapabilities:
        return AdapterCapabilities(
            supports_search=False,
            supports_get_product=False,
            supports_get_price=True,  # a real, licensed price source
            supports_get_availability=False,
            supports_store_catalog=True,
            requires_network=True,
            is_community=False,
            default_source_type=CF_SOURCE_TYPE,
            retailers=(),
        )

    def metadata(self) -> AdapterMetadata:
        return AdapterMetadata(
            adapter_key=self.adapter_key,
            version="1.0",
            source_type=self.source_type,
            status=AdapterStatus.EXPERIMENTAL,
            # Registry-visible enabled reflects the presence of base_url + key + mapping; the
            # runtime gate additionally ANDs the DataSource.is_enabled flag (see the sync).
            enabled=self._config.configured,
            data_source_slug=CF_DATA_SOURCE_SLUG,
            license_code=self._config.license_code,
            attribution_text=self._config.attribution,
        )

    # --- HTTP ---------------------------------------------------------------- #
    def _get(self, params: dict[str, Any]) -> httpx.Response | None:
        """Issue one GET to the products endpoint; ``None`` on any HTTP/transport error."""
        url = f"{self._config.base_url.rstrip('/')}/{self._config.products_path.lstrip('/')}"
        headers = {"User-Agent": CF_USER_AGENT, **self._config.auth()}
        try:
            if self._client is not None:
                return self._client.get(url, params=params, headers=headers)
            with httpx.Client(timeout=CF_TIMEOUT_SECONDS) as client:
                return client.get(url, params=params, headers=headers)
        except httpx.HTTPError:
            return None

    def _extract_items(self, payload: Any) -> list[dict[str, Any]]:
        """Locate the array of product items in a provider payload (config path or common keys)."""
        if isinstance(payload, list):
            data: Any = payload
        elif isinstance(payload, dict):
            if self._config.items_path:
                data = _dig(payload, self._config.items_path)
            else:
                data = None
                for key in ("items", "data", "products", "results"):
                    if isinstance(payload.get(key), list):
                        data = payload[key]
                        break
                if data is None:
                    data = payload.get("items")
        else:
            data = None
        return [it for it in data if isinstance(it, dict)] if isinstance(data, list) else []

    def _page_params(self, page: int) -> dict[str, Any]:
        style = self._config.pagination
        if style == "page":
            return {"page": page, "size": self._config.page_size}
        if style == "offset":
            return {"offset": (page - 1) * self._config.page_size, "limit": self._config.page_size}
        return {}

    # --- Mapping ------------------------------------------------------------- #
    def _map_record(
        self,
        item: dict[str, Any],
        *,
        retailer_slug: str,
        store_external_code: str,
        default_observed_at: datetime,
    ) -> NormalizedRecord | None:
        """Translate one provider item into a :class:`NormalizedRecord`, or ``None`` to skip.

        A row is skipped (never fabricated) when it lacks a usable price or any product identity
        (neither a barcode nor a provider product reference). Money is :class:`Decimal`.
        """
        fm = self._config.field_map

        def val(canonical: str) -> Any:
            path = fm.get(canonical)
            return _dig(item, path) if path else None

        amount = _decimal_or_none(val("amount"))
        if amount is None:
            return None

        barcode = _clean_str(val("barcode"))
        product_ref = _clean_str(val("product_ref"))
        external_id = barcode or product_ref
        if not external_id:
            return None  # no stable identity -> cannot key a real product

        observed_at = _parse_datetime(val("date")) or default_observed_at
        promo_price = _decimal_or_none(val("promo_price"))
        promotion = None
        if promo_price is not None and promo_price < amount:
            promotion = f"Precio promocionado {promo_price}"

        package_quantity = _decimal_or_none(val("package_quantity")) or Decimal("1")
        package_unit = _clean_str(val("package_unit")) or "unit"

        return NormalizedRecord(
            retailer_slug=retailer_slug,
            store_external_code=store_external_code,
            product_external_id=external_id,
            product_name=_clean_str(val("product_name")) or f"Producto {external_id}",
            package_quantity=package_quantity,
            package_unit=package_unit,
            amount=amount,
            currency=_clean_str(val("currency")) or "EUR",
            source_type=CF_SOURCE_TYPE,
            source_name=self._config.source_name,
            observed_at=observed_at,
            brand=_clean_str(val("brand")),
            category=_clean_str(val("category")),
            barcode=barcode,
            unit_price=_decimal_or_none(val("unit_price")),
            promotion=promotion,
            verification_status="unverified",
        )

    # --- Public API ---------------------------------------------------------- #
    def fetch_products(
        self,
        *,
        retailer_slug: str,
        store_external_code: str,
        default_observed_at: datetime | None = None,
    ) -> list[NormalizedRecord]:
        """Pull the licensed feed's priced products, mapped to canonical records (paginated).

        Returns the parsed records; on any network/HTTP/parse problem it stops and returns what
        it gathered so far (partial success, never a crash). An empty list is a valid, honest
        answer. Rows without a usable price or product identity are skipped, never fabricated.
        Refuses (empty) when the connector is unconfigured.
        """
        if not self._config.configured:
            return []
        default_observed_at = default_observed_at or datetime.now(UTC)

        collected: list[NormalizedRecord] = []
        page = 1
        while page <= CF_MAX_PAGES:
            response = self._get(self._page_params(page))
            if response is None or response.status_code != 200:
                break
            try:
                payload: Any = response.json()
            except (ValueError, UnicodeDecodeError):
                break
            items = self._extract_items(payload)
            if not items:
                break
            for item in items:
                record = self._map_record(
                    item,
                    retailer_slug=retailer_slug,
                    store_external_code=store_external_code,
                    default_observed_at=default_observed_at,
                )
                if record is not None:
                    collected.append(record)
            if self._config.pagination == "none":
                break
            if len(items) < self._config.page_size:
                break
            page += 1
        return collected
