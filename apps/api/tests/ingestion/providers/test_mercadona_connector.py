"""Mercadona DIRECT public-API connector (crawl transport + provider) — fully offline.

No network is ever touched: an ``httpx.MockTransport`` serves the change-pc, ``/categories/`` and
``/categories/{id}/`` endpoints, and ``sleep``/``delay`` are injected so the courtesy pacing is
asserted without real waiting. Covered: change-pc is POSTed with the configured postal code before
the crawl; the category tree -> subcategories -> products are walked and de-duplicated; the
inter-request courtesy delay is applied; the gating (flag + postal) turns the provider on/off;
``search=False`` / ``full_catalog=True`` capabilities; the monthly scheduler cadence; and that a
real sampled product maps byte-identically to the pre-existing Apify mapper output.
"""

from __future__ import annotations

import dataclasses
import json
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from cestaplan_api.config import Settings
from cestaplan_api.ingestion.contracts import PriceScope, RunType
from cestaplan_api.ingestion.providers.apify.mapping import ApifyMercadonaMapper
from cestaplan_api.ingestion.providers.contracts import ExternalCatalogProduct, ProductQuery
from cestaplan_api.ingestion.providers.exceptions import NotSupportedError
from cestaplan_api.ingestion.providers.mercadona.client import MercadonaClient
from cestaplan_api.ingestion.providers.mercadona.provider import MercadonaProvider
from cestaplan_api.ingestion.scheduler import SchedulerConfig

_FIXTURE = (
    Path(__file__).parents[2] / "fixtures" / "providers" / "apify-mercadona" / "sanitized.json"
)
_POSTAL = "28001"
_UA = "CestaPlanBot/1.0 (+https://example.test; price-ingestion)"


def _records() -> list[dict]:
    return json.loads(_FIXTURE.read_text())


def _tree() -> dict:
    # Two root categories; the leaves (11, 12) are the subcategories that carry products.
    return {
        "results": [
            {
                "id": 1,
                "name": "Root A",
                "categories": [
                    {"id": 11, "name": "Sub A1"},
                    {"id": 12, "name": "Sub A2"},
                ],
            }
        ]
    }


def _make_client(
    records: list[dict],
    *,
    postal_code: str = _POSTAL,
    change_pc_seen: list[str] | None = None,
    category_hits: list[int] | None = None,
    slept: list[float] | None = None,
) -> MercadonaClient:
    change_pc_seen = change_pc_seen if change_pc_seen is not None else []
    category_hits = category_hits if category_hits is not None else []
    slept = slept if slept is not None else []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path.endswith("/change-pc/"):
            change_pc_seen.append(json.loads(request.content)["new_postal_code"])
            return httpx.Response(200, json={"result": "ok"})
        if path.endswith("/api/categories/"):
            return httpx.Response(200, json=_tree())
        cid = int(path.rstrip("/").split("/")[-1])
        category_hits.append(cid)
        if cid == 11:  # top-level products
            return httpx.Response(200, json={"categories": [], "products": records[:1]})
        if cid == 12:  # products nested under a grouped sub-section
            return httpx.Response(
                200, json={"categories": [{"products": records[1:]}], "products": []}
            )
        return httpx.Response(200, json={"categories": [], "products": []})

    return MercadonaClient(
        postal_code=postal_code,
        user_agent=_UA,
        contact_email="ops@example.test",
        max_retries=3,
        delay_bounds_seconds=(0.5, 1.5),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=slept.append,
        delay=lambda lo, hi: 0.7,  # deterministic inside the [lo, hi] window
    )


# --- transport: change-pc + crawl ---------------------------------------- #
def test_change_pc_is_posted_with_configured_postal_before_crawl() -> None:
    seen: list[str] = []
    client = _make_client(_records(), change_pc_seen=seen)
    client.crawl_products()
    assert seen == [_POSTAL]  # exactly one change-pc, carrying the configured zone


def test_crawl_walks_subcategories_and_collects_all_products() -> None:
    hits: list[int] = []
    records = _make_client(_records(), category_hits=hits).crawl_products()
    assert hits == [11, 12]  # both leaf subcategories fetched, in order
    ids = [r["id"] for r in records]
    assert ids == [r["id"] for r in _records()]  # every product collected, top-level + nested


def test_crawl_dedupes_products_seen_under_multiple_categories() -> None:
    records = _records()
    # Force the SAME product into both subcategories; it must appear once.
    dup = records[0]

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST":
            return httpx.Response(200, json={"result": "ok"})
        if path.endswith("/api/categories/"):
            return httpx.Response(200, json=_tree())
        return httpx.Response(200, json={"categories": [], "products": [dup]})

    client = MercadonaClient(
        postal_code=_POSTAL,
        user_agent=_UA,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=lambda _s: None,
        delay=lambda lo, hi: 0.0,
    )
    collected = client.crawl_products()
    assert [r["id"] for r in collected] == [dup["id"]]  # deduped across categories


def test_courtesy_delay_applied_between_category_requests() -> None:
    slept: list[float] = []
    _make_client(_records(), slept=slept).crawl_products()
    # One delay per subcategory detail fetch (the /categories/ root read is not delayed).
    assert slept == [0.7, 0.7]
    assert all(0.5 <= s <= 1.5 for s in slept)  # inside the configured politeness window


def test_max_products_bounds_the_crawl() -> None:
    records = _make_client(_records()).crawl_products(max_products=1)
    assert len(records) == 1


def test_change_pc_noop_when_no_postal() -> None:
    seen: list[str] = []
    client = _make_client(_records(), postal_code="", change_pc_seen=seen)
    client.crawl_products()
    assert seen == []  # no zone configured -> no change-pc call


# --- mapper reuse: identical shape --------------------------------------- #
def test_crawled_products_map_identically_to_the_apify_mapper() -> None:
    records = _records()
    provider = MercadonaProvider(client=_make_client(records))
    got = list(provider.iterate_products(ProductQuery()))
    # The pre-existing mapper on the same records is the ground truth (observed_at is the only
    # value that legitimately differs — it is the retrieval time each path stamps).
    expected = ApifyMercadonaMapper().map_products(
        records, postal_code=_POSTAL, observed_at=got[0].observed_at
    )
    assert len(got) == len(records) == 25

    def normalise(product: ExternalCatalogProduct) -> ExternalCatalogProduct:
        return dataclasses.replace(product, observed_at=got[0].observed_at)

    assert [normalise(p) for p in got] == [normalise(p) for p in expected]
    garrafa = next(p for p in got if p.external_product_id == "4241")
    assert garrafa.provider == "apify-mercadona"  # LIVE provider code preserved
    assert garrafa.regular_price == Decimal("17.75")
    assert garrafa.price_scope is PriceScope.POSTAL_CODE and garrafa.postal_code == _POSTAL


def test_search_term_filters_by_name_client_side() -> None:
    provider = MercadonaProvider(client=_make_client(_records()))
    got = list(provider.iterate_products(ProductQuery(search="balsamic")))
    assert got  # at least the balsamic cream
    assert all("balsamic" in p.product_name.lower() for p in got)


# --- gating -------------------------------------------------------------- #
def test_provider_disabled_by_default_raises_on_iterate() -> None:
    provider = MercadonaProvider(settings=Settings())  # flag OFF by default
    assert provider.health_check().ok is False
    with pytest.raises(NotSupportedError):
        list(provider.iterate_products(ProductQuery()))


def test_provider_enabled_without_postal_stays_off() -> None:
    provider = MercadonaProvider(settings=Settings(mercadona_connector_enabled=True))
    assert provider.health_check().ok is False


def test_provider_enabled_with_postal_is_configured() -> None:
    provider = MercadonaProvider(
        settings=Settings(mercadona_connector_enabled=True, mercadona_postal_code=_POSTAL)
    )
    assert provider.health_check().ok is True


def test_provider_falls_back_to_legacy_postal_setting() -> None:
    provider = MercadonaProvider(
        settings=Settings(
            mercadona_connector_enabled=True, apify_mercadona_default_postal_code="14006"
        )
    )
    assert provider.health_check().ok is True


# --- capabilities + metadata --------------------------------------------- #
def test_capabilities_are_full_catalog_not_search() -> None:
    caps = MercadonaProvider(settings=Settings()).capabilities()
    assert caps.search is False  # no text endpoint -> catalogue/staged-reuse discovery
    assert caps.full_catalog is True and caps.store_scope is True


def test_metadata_is_honest_non_official() -> None:
    meta = MercadonaProvider(settings=Settings()).get_source_metadata()
    assert meta.provider_code == "apify-mercadona"
    assert meta.retailer_slug == "mercadona"
    assert meta.official is False


# --- monthly cadence ----------------------------------------------------- #
def test_mercadona_scheduler_cadence_is_monthly() -> None:
    cfg = SchedulerConfig().for_retailer("mercadona")
    assert cfg.cadence_for(RunType.CATALOG) == 30
    assert cfg.cadence_for(RunType.PRICES) == 30
    # A different retailer still uses the default (non-monthly) cadence.
    assert SchedulerConfig().for_retailer("dia").cadence_for(RunType.PRICES) == 1
