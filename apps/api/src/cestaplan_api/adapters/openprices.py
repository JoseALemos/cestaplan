"""Open Food Facts **Open Prices** adapter — real, community-observed prices (ODbL).

Unlike the Open Food Facts product adapter (which is *never* a price source), Open Prices
*is* a price source: it is an open, ODbL-licensed database of real prices that contributors
photograph from receipts and shelf tags, addressable by OpenStreetMap store location. This
adapter reads the official public API (``https://prices.openfoodfacts.org/api/v1``, no auth
for reads) with a descriptive ``User-Agent`` and a bounded timeout. No scraping, no anti-bot
evasion, no fabrication: a price is only ever what the API returns, missing fields stay
``None`` and every failure mode (network error, timeout, non-200, malformed payload)
degrades gracefully to the prices gathered so far — the adapter never crashes and never
invents data.

The unit of work is :meth:`OpenPricesAdapter.fetch_store_prices` — pull every price observed
at one OSM store location (paginated). Each row is parsed into a plain :class:`OpenPrice`
value object the sync service (Task 3) turns into a canonical ``ProductPrice`` observation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from cestaplan_api.adapters.base import (
    AdapterCapabilities,
    AdapterMetadata,
    AdapterStatus,
    RetailerAdapter,
)

#: Base of the public Open Prices read API (v1). Only read endpoints are used.
OP_API_BASE = "https://prices.openfoodfacts.org/api/v1"
#: Public Open Prices site (stored as the source/attribution anchor).
OP_SITE_URL = "https://prices.openfoodfacts.org"
#: Public price page, stored per observation as its ``source_url`` (ODbL provenance).
OP_PRICE_PAGE_URL = "https://prices.openfoodfacts.org/prices/{price_id}"
#: Descriptive User-Agent per Open Food Facts usage guidelines.
OP_USER_AGENT = "CestaPlan/0.0 (+self-hosted)"
#: Bounded timeout (seconds) for every Open Prices read; never blocks unbounded.
OP_TIMEOUT_SECONDS = 15.0
#: Page size for the paginated ``/prices`` endpoint.
OP_PAGE_SIZE = 100
#: Safety cap on pages pulled per store (defensive; real stores have very few pages).
OP_MAX_PAGES = 50
#: Safety cap on ``/locations`` pages during live discovery (the whole global list is
#: paginated and filtered client-side; ~65 pages of 100 at the time of writing).
OP_MAX_LOCATION_PAGES = 500

OP_ADAPTER_KEY = "open_prices"
OP_DATA_SOURCE_SLUG = "open-prices"
OP_SOURCE_NAME = "Open Food Facts - Open Prices"
OP_LICENSE_CODE = "ODbL"
OP_ATTRIBUTION_TEXT = (
    "Precios de Open Food Facts - Open Prices, bajo licencia ODbL. "
    "https://prices.openfoodfacts.org"
)


@dataclass(slots=True)
class OpenPrice:
    """One real price observation from Open Prices, parsed and normalized.

    ``amount`` is the observed price (a :class:`~decimal.Decimal`, never a float);
    ``barcode`` is the product EAN/UPC (``product_code``) or ``None`` for category/loose
    items the sync service skips. ``price_per`` is ``UNIT`` / ``KILOGRAM`` / ``None`` and
    lets the sync derive a base ``unit_price`` when meaningful — otherwise it stays ``None``.
    """

    price_id: int
    amount: Decimal
    currency: str
    observed_on: date
    source_url: str
    location_osm_id: int | None = None
    location_osm_type: str | None = None
    barcode: str | None = None
    product_name: str | None = None
    price_per: str | None = None
    price_is_discounted: bool = False
    price_without_discount: Decimal | None = None


@dataclass(slots=True)
class OpenPricesLocation:
    """One OpenStreetMap store location known to Open Prices, parsed and normalized.

    ``price_count`` is how many real price observations Open Prices holds for this location;
    only locations with ``price_count > 0`` are worth seeding as a real ``Store``. Address
    fields come straight from OSM (``osm_address_*``); missing values stay ``None``.
    """

    osm_id: int
    osm_type: str
    price_count: int
    osm_name: str | None = None
    osm_brand: str | None = None
    country_code: str | None = None
    city: str | None = None
    postcode: str | None = None
    latitude: Decimal | None = None
    longitude: Decimal | None = None


def _parse_location(item: dict[str, Any]) -> OpenPricesLocation | None:
    """Parse one ``/locations`` item into an :class:`OpenPricesLocation`, or ``None``.

    A location without a usable ``osm_id`` / ``osm_type`` cannot be addressed as a store and
    is skipped. ``price_count`` degrades to 0 (an honest "no prices yet"), never fabricated.
    """
    osm_id = item.get("osm_id")
    osm_type = _clean_str(item.get("osm_type"))
    if not isinstance(osm_id, int) or not osm_type:
        return None
    price_count = item.get("price_count")
    return OpenPricesLocation(
        osm_id=osm_id,
        osm_type=osm_type.upper(),
        price_count=price_count if isinstance(price_count, int) else 0,
        osm_name=_clean_str(item.get("osm_name")),
        osm_brand=_clean_str(item.get("osm_brand")),
        country_code=_clean_str(item.get("osm_address_country_code")),
        city=_clean_str(item.get("osm_address_city")),
        postcode=_clean_str(item.get("osm_address_postcode")),
        latitude=_decimal_or_none(item.get("osm_lat")),
        longitude=_decimal_or_none(item.get("osm_lon")),
    )


def _decimal_or_none(value: Any) -> Decimal | None:
    """Parse an Open Prices numeric (number or numeric string) to Decimal; never fabricate."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str) and not value.strip():
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _clean_str(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _parse_date(value: Any) -> date | None:
    text = value.strip() if isinstance(value, str) else ""
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _parse_price(item: dict[str, Any]) -> OpenPrice | None:
    """Parse one ``/prices`` item into an :class:`OpenPrice`, or ``None`` if unusable.

    A row without a numeric ``price`` or a valid ``date`` cannot be a price observation and
    is skipped (absence is never turned into 0). Everything else degrades to ``None``.
    """
    price_id = item.get("id")
    if not isinstance(price_id, int):
        return None
    amount = _decimal_or_none(item.get("price"))
    if amount is None:
        return None
    observed_on = _parse_date(item.get("date"))
    if observed_on is None:
        return None

    location = item.get("location")
    loc: dict[str, Any] = location if isinstance(location, dict) else {}
    return OpenPrice(
        price_id=price_id,
        amount=amount,
        currency=_clean_str(item.get("currency")) or "EUR",
        observed_on=observed_on,
        source_url=OP_PRICE_PAGE_URL.format(price_id=price_id),
        location_osm_id=item.get("location_osm_id") or loc.get("osm_id"),
        location_osm_type=_clean_str(item.get("location_osm_type"))
        or _clean_str(loc.get("osm_type")),
        barcode=_clean_str(item.get("product_code")),
        product_name=_clean_str(item.get("product_name")),
        price_per=_clean_str(item.get("price_per")),
        price_is_discounted=bool(item.get("price_is_discounted")),
        price_without_discount=_decimal_or_none(item.get("price_without_discount")),
    )


class OpenPricesAdapter(RetailerAdapter):
    """Read adapter over the Open Food Facts Open Prices API. **A real price source.**

    Its capability is a store-wide price pull via :meth:`fetch_store_prices`; the canonical
    single-product ``RetailerAdapter`` read methods stay unsupported because Open Prices is
    queried by store location, not by a per-product/selector reference.
    """

    adapter_key = OP_ADAPTER_KEY
    source_type = "open_dataset"
    enabled = True

    def __init__(self, client: httpx.Client | None = None) -> None:
        # An injected client (e.g. an httpx.MockTransport client in tests) is reused and not
        # closed here; when absent, each call opens and closes a short-lived client.
        self._client = client

    def capabilities(self) -> AdapterCapabilities:
        return AdapterCapabilities(
            supports_search=False,
            supports_get_product=False,
            supports_get_price=True,  # Open Prices DOES provide real prices.
            supports_get_availability=False,
            supports_store_catalog=True,
            requires_network=True,
            is_community=True,
            default_source_type="open_dataset",
            retailers=(),
        )

    def metadata(self) -> AdapterMetadata:
        return AdapterMetadata(
            adapter_key=self.adapter_key,
            version="1.0",
            source_type=self.source_type,
            status=AdapterStatus.ACTIVE,
            enabled=self.enabled,
            data_source_slug=OP_DATA_SOURCE_SLUG,
            license_code=OP_LICENSE_CODE,
            attribution_text=OP_ATTRIBUTION_TEXT,
        )

    def _get(self, params: dict[str, Any], *, path: str = "/prices") -> httpx.Response | None:
        """Issue one GET to ``path`` (default ``/prices``); ``None`` on any HTTP error."""
        url = f"{OP_API_BASE}{path}"
        headers = {"User-Agent": OP_USER_AGENT}
        try:
            if self._client is not None:
                return self._client.get(url, params=params, headers=headers)
            with httpx.Client(timeout=OP_TIMEOUT_SECONDS) as client:
                return client.get(url, params=params, headers=headers)
        except httpx.HTTPError:
            return None

    def fetch_store_prices(self, osm_id: int, osm_type: str) -> list[OpenPrice]:
        """Pull every Open Prices observation at one OSM store location (paginated).

        Returns the parsed prices; on any network/HTTP/parse problem the method stops and
        returns whatever it gathered so far (partial success, never a crash). An empty list
        is a valid, honest answer (the store simply has no prices yet) — never fabricated.
        """
        collected: list[OpenPrice] = []
        page = 1
        while page <= OP_MAX_PAGES:
            response = self._get(
                {
                    "location_osm_id": osm_id,
                    "location_osm_type": osm_type,
                    "size": OP_PAGE_SIZE,
                    "page": page,
                }
            )
            if response is None or response.status_code != 200:
                break
            try:
                payload: Any = response.json()
            except (ValueError, UnicodeDecodeError):
                break
            if not isinstance(payload, dict):
                break
            items = payload.get("items")
            if not isinstance(items, list) or not items:
                break
            for item in items:
                if isinstance(item, dict):
                    parsed = _parse_price(item)
                    if parsed is not None:
                        collected.append(parsed)
            pages = payload.get("pages")
            if not isinstance(pages, int) or page >= pages:
                break
            page += 1
        return collected

    def fetch_locations(self, country_code: str = "ES") -> list[OpenPricesLocation]:
        """Pull every Open Prices store location for a country (paginated, client-side filter).

        Open Prices ignores the country query filter server-side, so the whole ``/locations``
        list is paginated and filtered here by ``osm_address_country_code``. Returns the parsed
        locations (any ``price_count``); on any network/HTTP/parse problem it stops and returns
        whatever it gathered so far (partial success, never a crash, never fabricated data).
        """
        wanted = country_code.strip().upper()
        collected: list[OpenPricesLocation] = []
        page = 1
        while page <= OP_MAX_LOCATION_PAGES:
            response = self._get(
                {"size": OP_PAGE_SIZE, "page": page}, path="/locations"
            )
            if response is None or response.status_code != 200:
                break
            try:
                payload: Any = response.json()
            except (ValueError, UnicodeDecodeError):
                break
            if not isinstance(payload, dict):
                break
            items = payload.get("items")
            if not isinstance(items, list) or not items:
                break
            for item in items:
                if isinstance(item, dict):
                    parsed = _parse_location(item)
                    if parsed is not None and (parsed.country_code or "").upper() == wanted:
                        collected.append(parsed)
            pages = payload.get("pages")
            if not isinstance(pages, int) or page >= pages:
                break
            page += 1
        return collected
