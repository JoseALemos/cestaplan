"""Price-provider registry (FASE 1).

Maps a stable ``provider_code`` to a factory that builds a
:class:`~cestaplan_api.ingestion.providers.contracts.PriceCatalogProvider`. Providers are
registered by their own modules (Parse.bot / Apify / Open Prices in later phases); the demo
provider is always registered. Enablement is driven by feature flags at the call site — the
registry only knows how to build a provider, not whether it should run.
"""

from __future__ import annotations

from collections.abc import Callable

from cestaplan_api.ingestion.providers.contracts import (
    PriceCatalogProvider,
    ProviderMetadata,
)
from cestaplan_api.ingestion.providers.exceptions import NotSupportedError

ProviderFactory = Callable[[], PriceCatalogProvider]


class ProviderRegistry:
    """A small, explicit registry of provider factories keyed by ``provider_code``."""

    def __init__(self) -> None:
        self._factories: dict[str, ProviderFactory] = {}

    def register(self, code: str, factory: ProviderFactory) -> None:
        self._factories[code] = factory

    def codes(self) -> list[str]:
        return sorted(self._factories)

    def has(self, code: str) -> bool:
        return code in self._factories

    def get(self, code: str) -> PriceCatalogProvider:
        try:
            return self._factories[code]()
        except KeyError as exc:
            raise NotSupportedError(f"unknown provider {code!r}") from exc

    def metadata(self) -> list[ProviderMetadata]:
        return [self.get(code).get_source_metadata() for code in self.codes()]


#: The process-wide registry. Provider modules call ``register_default(...)`` at import.
registry = ProviderRegistry()


def register_default(code: str, factory: ProviderFactory) -> None:
    registry.register(code, factory)


# Demo is always available (no flags, no network).
from cestaplan_api.ingestion.providers.demo.provider import DemoCatalogProvider  # noqa: E402

register_default(DemoCatalogProvider.provider_code, DemoCatalogProvider)

__all__ = ["ProviderRegistry", "register_default", "registry"]
