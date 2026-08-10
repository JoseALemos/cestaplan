"""Mercadona price-catalog provider — DIRECT, FREE crawl of Mercadona's public store API.

Replaces the paid Apify actor path while keeping the provider code ``apify-mercadona`` so the 64
LIVE ingredient mappings, discovery candidates and 161 prices captured under that code are
preserved unchanged. The record shape returned by the public API is identical to the actor's, so
the existing :class:`ApifyMercadonaMapper` is REUSED verbatim (same fields, same schema
fingerprint) — no mapping logic is duplicated.

Gating: OFF by default. The crawl only runs when ``mercadona_connector_enabled`` is set AND a
postal code is configured (``mercadona_postal_code``, falling back to the legacy
``apify_mercadona_default_postal_code``). No Apify token is involved on this path.

Cadence: Mercadona is refreshed MONTHLY (owner request — more courteous than the old twice a
week). The 30-day spacing is configured in :mod:`cestaplan_api.ingestion.scheduler`
(``per_retailer`` entry for ``mercadona``), since cadence is a scheduler concern, not a provider.

Capabilities: ``search=False`` — there is no text-search endpoint, so discovery uses the
full-catalogue / staged-reuse route (a monthly crawl), not per-ingredient search. A ``search`` term
on :meth:`iterate_products`, if given, is honoured as a best-effort client-side name filter.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

from cestaplan_api.config import Settings, get_settings
from cestaplan_api.ingestion.providers.apify.mapping import ApifyMercadonaMapper
from cestaplan_api.ingestion.providers.contracts import (
    ExternalCatalogProduct,
    HealthStatus,
    PriceCatalogProvider,
    ProductQuery,
    ProviderCapabilities,
    ProviderKind,
    ProviderMetadata,
    ProviderStatus,
)
from cestaplan_api.ingestion.providers.exceptions import NotSupportedError
from cestaplan_api.ingestion.providers.mercadona.client import MercadonaClient


class MercadonaProvider(PriceCatalogProvider):
    provider_code = "apify-mercadona"  # kept stable: LIVE mappings/candidates/prices key off it

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        client: MercadonaClient | None = None,
        mapper: ApifyMercadonaMapper | None = None,
    ) -> None:
        s = settings or get_settings()
        self._settings = s
        self._mapper = mapper or ApifyMercadonaMapper()
        postal = s.mercadona_postal_code or s.apify_mercadona_default_postal_code
        if client is not None:
            self._client: MercadonaClient | None = client
        elif s.mercadona_connector_enabled and postal:
            self._client = MercadonaClient(
                postal_code=postal,
                user_agent=s.scraping_user_agent,
                contact_email=s.scraping_contact_email,
                timeout=float(s.scraping_timeout_seconds),
                max_retries=s.scraping_max_retries,
                delay_bounds_seconds=s.scraping_request_delay_bounds_seconds,
            )
        else:
            self._client = None

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            full_catalog=True,  # a full category crawl of the configured zone
            store_scope=True,  # postal-code (delivery-zone) scoped prices
            incremental_sync=False,
            promotions=True,
            categories=True,
            search=False,  # no text-search endpoint -> full-catalogue/staged-reuse discovery
        )

    def get_source_metadata(self) -> ProviderMetadata:
        return ProviderMetadata(
            provider_code=self.provider_code,
            retailer_slug="mercadona",
            kind=ProviderKind.INDEPENDENT,  # public store API read directly; not an official API
            status=ProviderStatus.ACTIVE_WHEN_CONFIGURED,
            official=False,
            catalog_type="full_crawl",
            attribution=(
                "API pública de Mercadona (tienda.mercadona.es). Acceso directo, cortés "
                "(User-Agent identificable, rate-limit, cadencia mensual). No es una API oficial "
                "documentada ni evade bloqueos."
            ),
        )

    def health_check(self) -> HealthStatus:
        # No cheap unauthenticated ping is made speculatively; report the honest configured state.
        if self._client is None:
            return HealthStatus(ok=False, detail="mercadona direct connector not configured")
        return HealthStatus(
            ok=True, detail="mercadona direct connector configured", checked_at=datetime.now(UTC)
        )

    def iterate_products(self, query: ProductQuery) -> Iterator[ExternalCatalogProduct]:
        if self._client is None:
            raise NotSupportedError(
                "mercadona direct connector not configured (enable flag + postal code)"
            )
        records = self._client.crawl_products(max_products=query.max_products)
        if query.search:  # no search endpoint: best-effort client-side name filter when requested
            term = query.search.strip().lower()
            records = [r for r in records if term in str(r.get("display_name", "")).lower()]
        # Stamp the zone we actually pinned (never a different one the query asked for) so a price
        # is never cross-zoned — the mapper reads this to seal the postal-code scope.
        postal = self._client.postal_code or None
        observed_at = datetime.now(UTC)
        yield from self._mapper.map_products(
            records, postal_code=postal, observed_at=observed_at
        )


__all__ = ["MercadonaProvider"]
