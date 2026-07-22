"""Adapter registry: ``adapter_key`` → adapter, with listing and status introspection.

Community/experimental and skeleton adapters are registered but ship disabled; the "Estado
de fuentes" admin view reads :func:`list_adapters` to show what is available and what is off.
"""

from __future__ import annotations

from dataclasses import dataclass

from cestaplan_api.adapters.base import (
    AdapterCapabilities,
    AdapterStatus,
    RetailerAdapter,
)
from cestaplan_api.adapters.demo import DemoRetailerAdapter
from cestaplan_api.adapters.files import (
    CsvRetailerAdapter,
    JsonRetailerAdapter,
    ManualRetailerAdapter,
)
from cestaplan_api.adapters.openfoodfacts import OpenFoodFactsAdapter
from cestaplan_api.adapters.skeletons import (
    AlcampoAdapter,
    AldiAdapter,
    CarrefourAdapter,
    DezaAdapter,
    DiaAdapter,
    LidlAdapter,
    MercadonaCommunityAdapter,
)


def _build_registry() -> dict[str, RetailerAdapter]:
    adapters: list[RetailerAdapter] = [
        DemoRetailerAdapter(),
        CsvRetailerAdapter(),
        JsonRetailerAdapter(),
        ManualRetailerAdapter(),
        OpenFoodFactsAdapter(),
        MercadonaCommunityAdapter(),
        AldiAdapter(),
        LidlAdapter(),
        CarrefourAdapter(),
        DiaAdapter(),
        AlcampoAdapter(),
        DezaAdapter(),
    ]
    return {adapter.adapter_key: adapter for adapter in adapters}


#: Canonical registry of every known adapter, keyed by ``adapter_key``.
ADAPTER_REGISTRY: dict[str, RetailerAdapter] = _build_registry()


def get_adapter(adapter_key: str) -> RetailerAdapter | None:
    """Look up an adapter by key, or ``None`` if unknown."""
    return ADAPTER_REGISTRY.get(adapter_key)


@dataclass(frozen=True, slots=True)
class AdapterListing:
    """A flattened view of an adapter's identity, status and capabilities."""

    adapter_key: str
    version: str
    source_type: str | None
    status: AdapterStatus
    enabled: bool
    is_community: bool
    requires_network: bool
    retailers: tuple[str, ...]
    license_code: str | None
    attribution_text: str | None
    data_source_slug: str | None
    capabilities: AdapterCapabilities


def list_adapters() -> list[AdapterListing]:
    """List every registered adapter with its enabled/disabled status and capabilities."""
    listings: list[AdapterListing] = []
    for adapter in ADAPTER_REGISTRY.values():
        meta = adapter.metadata()
        caps = adapter.capabilities()
        listings.append(
            AdapterListing(
                adapter_key=meta.adapter_key,
                version=meta.version,
                source_type=meta.source_type,
                status=meta.status,
                enabled=meta.enabled,
                is_community=caps.is_community,
                requires_network=caps.requires_network,
                retailers=caps.retailers,
                license_code=meta.license_code,
                attribution_text=meta.attribution_text,
                data_source_slug=meta.data_source_slug,
                capabilities=caps,
            )
        )
    return listings
