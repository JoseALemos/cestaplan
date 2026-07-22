"""Adapter skeletons and the experimental community connector.

These fix the contract for chains the model supports without providing any data. The chain
skeletons are inert (``NotImplementedError``); the Mercadona community connector is
experimental and **disabled by default** — it must be enabled explicitly and, per the
canonical rules, never scrapes or evades anti-bot protection.

The existence of a skeleton or a community connector does NOT imply real prices for that
chain are available.
"""

from __future__ import annotations

from cestaplan_api.adapters.base import (
    AdapterCapabilities,
    AdapterMetadata,
    AdapterStatus,
    NormalizedRecord,
    RetailerAdapter,
    StoreSelector,
)


class _ChainSkeleton(RetailerAdapter):
    """Base for inert chain skeletons: declares identity, implements nothing."""

    source_type = None
    enabled = False
    _retailer_slug: str = ""

    def capabilities(self) -> AdapterCapabilities:
        return AdapterCapabilities(
            requires_network=False,
            retailers=(self._retailer_slug,) if self._retailer_slug else (),
        )

    def metadata(self) -> AdapterMetadata:
        return AdapterMetadata(
            adapter_key=self.adapter_key,
            version="0.0",
            source_type=self.source_type,
            status=AdapterStatus.SKELETON,
            enabled=self.enabled,
        )

    def get_store_catalog(
        self, selector: StoreSelector, cursor: str | None = None
    ) -> list[NormalizedRecord]:
        raise NotImplementedError(
            f"{self.adapter_key}: adaptador esqueleto sin implementación"
        )


class AldiAdapter(_ChainSkeleton):
    adapter_key = "aldi"
    _retailer_slug = "aldi"


class LidlAdapter(_ChainSkeleton):
    adapter_key = "lidl"
    _retailer_slug = "lidl"


class CarrefourAdapter(_ChainSkeleton):
    adapter_key = "carrefour"
    _retailer_slug = "carrefour"


class DiaAdapter(_ChainSkeleton):
    adapter_key = "dia"
    _retailer_slug = "dia"


class AlcampoAdapter(_ChainSkeleton):
    adapter_key = "alcampo"
    _retailer_slug = "alcampo"


class DezaAdapter(_ChainSkeleton):
    adapter_key = "deza"
    _retailer_slug = "deza"


class MercadonaCommunityAdapter(RetailerAdapter):
    """Experimental community connector for Mercadona. **Disabled by default.**

    Ships ``enabled=False`` and must be enabled explicitly by whoever deploys, under their
    own responsibility. It performs no scraping and evades no anti-bot mechanism; the read
    methods are intentionally unimplemented in the MVP.
    """

    adapter_key = "mercadona_community"
    source_type = "community_connector"
    enabled = False

    def capabilities(self) -> AdapterCapabilities:
        return AdapterCapabilities(
            supports_get_price=False,
            requires_network=True,
            is_community=True,
            default_source_type="community_connector",
            retailers=("mercadona",),
        )

    def metadata(self) -> AdapterMetadata:
        return AdapterMetadata(
            adapter_key=self.adapter_key,
            version="0.0",
            source_type=self.source_type,
            status=AdapterStatus.EXPERIMENTAL,
            enabled=self.enabled,
            license_code="proprietary",
        )

    def get_price(
        self, product_ref: str, selector: StoreSelector
    ) -> NormalizedRecord | None:
        raise NotImplementedError(
            "mercadona_community: conector comunitario experimental desactivado; "
            "sin scraping ni elusión anti-bot"
        )
