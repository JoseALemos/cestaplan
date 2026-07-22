"""Open Food Facts (OFF) adapter — product enrichment only, **never prices**.

OFF is an open dataset (``source_type='open_dataset'``, ODbL 1.0). CestaPlan uses it *only*
for product data: barcode lookup, declared ingredients, declared allergens, nutrition,
categories, brands and a product image reference. It is **never** a price source and this
adapter reads/stores no price whatsoever (see docs/DATA_SOURCES.md §3, §4).

Access is through the official OFF read API v2 (``/api/v2/product/{barcode}.json``) with a
descriptive ``User-Agent`` per OFF guidelines and a bounded timeout. No scraping, no anti-bot
evasion. Every failure mode — network error, timeout, 404, product-not-found or a malformed
payload — degrades gracefully to ``None`` (a clear "not found / unavailable"); the adapter
never crashes the request and never fabricates data.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from cestaplan_api.adapters.base import (
    AdapterCapabilities,
    AdapterMetadata,
    AdapterStatus,
    RetailerAdapter,
)

#: Base of the official OFF read API. Only the read endpoint is used.
OFF_API_BASE = "https://world.openfoodfacts.org"
#: Public product page, stored as the ODbL source URL / attribution anchor.
OFF_PRODUCT_URL = "https://world.openfoodfacts.org/product/{barcode}"
#: Descriptive User-Agent required by OFF's usage guidelines.
OFF_USER_AGENT = "CestaPlan/0.0 (+self-hosted)"
#: Bounded timeout (seconds) for every OFF read; OFF never blocks the request unbounded.
OFF_TIMEOUT_SECONDS = 10.0

OFF_ADAPTER_KEY = "openfoodfacts"
OFF_DATA_SOURCE_SLUG = "openfoodfacts"
OFF_LICENSE_CODE = "ODbL"
OFF_ATTRIBUTION_TEXT = (
    "Datos de Open Food Facts, disponibles bajo Open Database License (ODbL) 1.0. "
    "Fuente: https://openfoodfacts.org — Licencia: https://opendatacommons.org/licenses/odbl/1-0/"
)

#: OFF allergen/traces tags (language prefix stripped, e.g. ``en:gluten`` → ``gluten``) mapped
#: to CestaPlan's canonical allergen codes (the snake_case vocabulary used by ``Ingredient``
#: and ``ProductNutrition``). Unmapped tags are kept as their stripped slug so a *declared*
#: allergen is never silently dropped before the deterministic allergen validation.
OFF_ALLERGEN_MAP: dict[str, str] = {
    "gluten": "gluten",
    "milk": "milk",
    "eggs": "egg",
    "egg": "egg",
    "fish": "fish",
    "peanuts": "peanut",
    "peanut": "peanut",
    "nuts": "tree_nut",
    "tree-nuts": "tree_nut",
    "soybeans": "soy",
    "soy": "soy",
    "sesame-seeds": "sesame",
    "sesame": "sesame",
    "crustaceans": "crustacean",
    "molluscs": "mollusc",
    "celery": "celery",
    "mustard": "mustard",
    "sulphur-dioxide-and-sulphites": "sulphite",
    "sulphites": "sulphite",
    "lupin": "lupin",
}


@dataclass(slots=True)
class OffProduct:
    """Parsed OFF product — product data only. There is **no price field** by design."""

    barcode: str
    source_url: str
    product_name: str | None = None
    brands: str | None = None
    categories: tuple[str, ...] = ()
    category_code: str | None = None
    ingredients_text: str | None = None
    allergens: tuple[str, ...] = ()
    traces: tuple[str, ...] = ()
    image_url: str | None = None
    # nutrition per 100 g / 100 ml (OFF's ``*_100g`` fields); missing → None, never 0.
    energy_kcal: Decimal | None = None
    protein_g: Decimal | None = None
    carbohydrate_g: Decimal | None = None
    sugars_g: Decimal | None = None
    fat_g: Decimal | None = None
    saturated_fat_g: Decimal | None = None
    fiber_g: Decimal | None = None
    salt_g: Decimal | None = None

    def to_public_dict(self) -> dict[str, Any]:
        """JSON-safe view (Decimals as strings) for the admin API response."""

        def s(value: Decimal | None) -> str | None:
            return str(value) if value is not None else None

        return {
            "barcode": self.barcode,
            "product_name": self.product_name,
            "brands": self.brands,
            "categories": list(self.categories),
            "category_code": self.category_code,
            "ingredients_text": self.ingredients_text,
            "allergens": list(self.allergens),
            "traces": list(self.traces),
            "image_url": self.image_url,
            "nutriments": {
                "energy_kcal_100g": s(self.energy_kcal),
                "proteins_100g": s(self.protein_g),
                "carbohydrates_100g": s(self.carbohydrate_g),
                "sugars_100g": s(self.sugars_g),
                "fat_100g": s(self.fat_g),
                "saturated_fat_100g": s(self.saturated_fat_g),
                "fiber_100g": s(self.fiber_g),
                "salt_100g": s(self.salt_g),
            },
        }


def _strip_lang_prefix(tag: str) -> str:
    """``en:gluten`` → ``gluten``; a bare ``gluten`` is returned unchanged."""
    return tag.split(":", 1)[1] if ":" in tag else tag


def _map_allergen_tags(tags: Any) -> tuple[str, ...]:
    """Map OFF allergen/traces tags to canonical codes, de-duplicated and order-stable."""
    if not isinstance(tags, list):
        return ()
    out: list[str] = []
    for tag in tags:
        if not isinstance(tag, str) or not tag.strip():
            continue
        slug = _strip_lang_prefix(tag.strip().lower())
        code = OFF_ALLERGEN_MAP.get(slug, slug)
        if code and code not in out:
            out.append(code)
    return tuple(out)


def _decimal_or_none(value: Any) -> Decimal | None:
    """Parse an OFF numeric (number or numeric string) to Decimal; never fabricate a 0."""
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


def _categories(product: dict[str, Any]) -> tuple[tuple[str, ...], str | None]:
    tags = product.get("categories_tags")
    if not isinstance(tags, list):
        return (), None
    cats = tuple(_strip_lang_prefix(t) for t in tags if isinstance(t, str) and t.strip())
    # OFF orders categories_tags from most generic to most specific → last is most specific.
    return cats, (cats[-1] if cats else None)


def _parse_product(barcode: str, product: dict[str, Any]) -> OffProduct:
    nutriments = product.get("nutriments")
    nut: dict[str, Any] = nutriments if isinstance(nutriments, dict) else {}
    categories, category_code = _categories(product)
    return OffProduct(
        barcode=barcode,
        source_url=OFF_PRODUCT_URL.format(barcode=barcode),
        product_name=_clean_str(product.get("product_name")),
        brands=_clean_str(product.get("brands")),
        categories=categories,
        category_code=category_code,
        ingredients_text=_clean_str(product.get("ingredients_text")),
        allergens=_map_allergen_tags(product.get("allergens_tags")),
        traces=_map_allergen_tags(product.get("traces_tags")),
        image_url=_clean_str(product.get("image_url"))
        or _clean_str(product.get("image_front_url")),
        energy_kcal=_decimal_or_none(nut.get("energy-kcal_100g")),
        protein_g=_decimal_or_none(nut.get("proteins_100g")),
        carbohydrate_g=_decimal_or_none(nut.get("carbohydrates_100g")),
        sugars_g=_decimal_or_none(nut.get("sugars_100g")),
        fat_g=_decimal_or_none(nut.get("fat_100g")),
        saturated_fat_g=_decimal_or_none(nut.get("saturated-fat_100g")),
        fiber_g=_decimal_or_none(nut.get("fiber_100g")),
        salt_g=_decimal_or_none(nut.get("salt_100g")),
    )


class OpenFoodFactsAdapter(RetailerAdapter):
    """Enrichment adapter over the Open Food Facts read API. **Never a price source.**

    Its capability is barcode lookup + product enrichment via :meth:`fetch_by_barcode`; the
    priced ``RetailerAdapter`` read methods (``get_product``/``get_price``/…) stay
    unsupported because OFF carries no price and a ``NormalizedRecord`` requires one.
    """

    adapter_key = OFF_ADAPTER_KEY
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
            supports_get_price=False,  # OFF is NEVER a price source.
            supports_get_availability=False,
            supports_store_catalog=False,
            requires_network=True,
            is_community=False,
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
            data_source_slug=OFF_DATA_SOURCE_SLUG,
            license_code=OFF_LICENSE_CODE,
            attribution_text=OFF_ATTRIBUTION_TEXT,
        )

    def fetch_by_barcode(self, barcode: str) -> OffProduct | None:
        """Look a barcode up on OFF, returning parsed product data or ``None``.

        ``None`` means "not found or source unavailable": a network error, timeout, 404,
        OFF ``status=0`` (product not found) or a malformed payload all degrade to ``None``.
        The method never raises for those and never invents data.
        """
        code = (barcode or "").strip()
        if not code:
            return None

        url = f"{OFF_API_BASE}/api/v2/product/{code}.json"
        headers = {"User-Agent": OFF_USER_AGENT}
        try:
            if self._client is not None:
                response = self._client.get(url, headers=headers)
            else:
                with httpx.Client(timeout=OFF_TIMEOUT_SECONDS) as client:
                    response = client.get(url, headers=headers)
        except httpx.HTTPError:
            # Connection error, timeout, protocol error… → unavailable, not a crash.
            return None

        if response.status_code == 404:
            return None
        if response.status_code != 200:
            return None

        try:
            payload: Any = response.json()
        except (ValueError, UnicodeDecodeError):
            return None
        if not isinstance(payload, dict):
            return None

        # OFF v2 signals not-found with status 0 (int) / "product not found" verbose.
        if payload.get("status") == 0:
            return None
        product = payload.get("product")
        if not isinstance(product, dict):
            return None

        return _parse_product(code, product)
