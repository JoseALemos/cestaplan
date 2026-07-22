"""Store adapters: the single ``RetailerAdapter`` contract and its implementations.

See ``docs/ADAPTER_GUIDE.md``. The deterministic engine and the import service talk only
to this contract, never to a concrete source. No adapter scrapes or evades anti-bot
protection; community connectors ship disabled by default.
"""

from __future__ import annotations

from cestaplan_api.adapters.base import (
    AdapterCapabilities,
    AdapterMetadata,
    AdapterStatus,
    NormalizedRecord,
    NotSupportedError,
    RawRow,
    RetailerAdapter,
    StoreSelector,
)
from cestaplan_api.adapters.openfoodfacts import OffProduct, OpenFoodFactsAdapter
from cestaplan_api.adapters.registry import (
    ADAPTER_REGISTRY,
    get_adapter,
    list_adapters,
)

__all__ = [
    "ADAPTER_REGISTRY",
    "AdapterCapabilities",
    "AdapterMetadata",
    "AdapterStatus",
    "NormalizedRecord",
    "NotSupportedError",
    "OffProduct",
    "OpenFoodFactsAdapter",
    "RawRow",
    "RetailerAdapter",
    "StoreSelector",
    "get_adapter",
    "list_adapters",
]
