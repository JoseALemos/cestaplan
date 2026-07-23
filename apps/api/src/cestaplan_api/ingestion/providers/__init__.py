"""Provider-agnostic price-catalog integration layer (FASE 1+).

Reuses the existing ingestion persistence/queue/worker; adds the richer
:class:`~cestaplan_api.ingestion.providers.contracts.PriceCatalogProvider` abstraction and
per-provider clients (Parse.bot, Apify, Open Prices, demo). Everything is feature-flagged
off by default; no provider is presented as an official retailer source.
"""

from cestaplan_api.ingestion.providers.contracts import (
    ExternalCatalogProduct,
    PriceCatalogProvider,
    ProviderCapabilities,
    ProviderMetadata,
)

__all__ = [
    "ExternalCatalogProduct",
    "PriceCatalogProvider",
    "ProviderCapabilities",
    "ProviderMetadata",
]
