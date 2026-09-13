"""DIA price-catalog provider — DIRECT, FREE search of DIA's public API.

Replaces the paid/scraper (Parse.bot) path while keeping the provider code ``parsebot-dia`` so the
rights registry, ingredient mappings, discovery candidates and prices captured under that code are
preserved unchanged — exactly as Mercadona kept ``apify-mercadona``. The mapping lives in
:class:`DiaMapper`; the transport in :class:`DiaClient`. No API key, no paid feed.

Gating: OFF by default. The connector only runs when ``dia_connector_enabled`` is set. No postal
code is required: v1 reads DIA's DEFAULT national warehouse (``store_scope=False``); store-scope
zone-pinning is a documented follow-up.

Cadence: DIA is refreshed MONTHLY (courteous direct public-API access, like Mercadona). The 30-day
spacing is configured in :mod:`cestaplan_api.ingestion.scheduler` (``per_retailer`` entry for
``dia``), since cadence is a scheduler concern, not a provider.

Capabilities: ``search=True`` (paginated text search), ``full_catalog=False`` (no full-catalogue
crawl), ``store_scope=False`` (national default zone).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

from cestaplan_api.config import Settings, get_settings
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
from cestaplan_api.ingestion.providers.dia.client import DiaClient
from cestaplan_api.ingestion.providers.dia.mapping import DiaMapper
from cestaplan_api.ingestion.providers.exceptions import NotSupportedError

_DEFAULT_QUERY = "leche"  # a bare ProductQuery() (no search term) uses a sane default term


class DiaProvider(PriceCatalogProvider):
    provider_code = "parsebot-dia"  # kept stable: rights/mappings/candidates/prices key off it

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        client: DiaClient | None = None,
        mapper: DiaMapper | None = None,
        query: str = _DEFAULT_QUERY,
    ) -> None:
        s = settings or get_settings()
        self._settings = s
        self._mapper = mapper or DiaMapper()
        self._query = query
        if client is not None:
            self._client: DiaClient | None = client
        elif s.dia_connector_enabled:
            self._client = DiaClient(
                # DIA-specific UA (its WAF blocks non-browser UAs); From contact header retained.
                # See config.dia_user_agent for the owner-authorized courtesy rationale.
                user_agent=s.dia_user_agent,
                contact_email=s.scraping_contact_email,
                timeout=float(s.scraping_timeout_seconds),
                max_retries=s.scraping_max_retries,
                delay_bounds_seconds=s.scraping_request_delay_bounds_seconds,
            )
        else:
            self._client = None

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            full_catalog=False,  # search-based; not a full catalogue crawl
            store_scope=False,  # v1: national default warehouse (zone-pinning is a follow-up)
            incremental_sync=False,
            promotions=True,
            categories=True,
            search=True,  # paginated text-search endpoint
        )

    def get_source_metadata(self) -> ProviderMetadata:
        return ProviderMetadata(
            provider_code=self.provider_code,
            retailer_slug="dia",
            kind=ProviderKind.INDEPENDENT,  # public API read directly; not an official DIA API
            status=ProviderStatus.ACTIVE_WHEN_CONFIGURED,
            official=False,
            catalog_type="search_direct",
            attribution=(
                "API pública de DIA (www.dia.es). Acceso directo, cortés (User-Agent "
                "identificable, rate-limit, cadencia mensual). No es una API oficial documentada "
                "ni evade bloqueos. Zona nacional por defecto."
            ),
        )

    def health_check(self) -> HealthStatus:
        # No cheap unauthenticated ping is made speculatively; report the honest configured state.
        if self._client is None:
            return HealthStatus(ok=False, detail="dia direct connector not configured")
        return HealthStatus(
            ok=True, detail="dia direct connector configured", checked_at=datetime.now(UTC)
        )

    def iterate_products(self, query: ProductQuery) -> Iterator[ExternalCatalogProduct]:
        if self._client is None:
            raise NotSupportedError("dia direct connector not configured (enable flag)")
        term = (query.search or self._query).strip() or self._query
        records = self._client.search(term, max_products=query.max_products)
        observed_at = datetime.now(UTC)
        yield from self._mapper.map_products(records, observed_at=observed_at)


__all__ = ["DiaProvider"]
