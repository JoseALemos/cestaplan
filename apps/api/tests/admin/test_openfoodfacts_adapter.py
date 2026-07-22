"""OpenFoodFactsAdapter tests — HTTPX fully mocked, NO network / NO real OFF calls.

Covers: parsing a valid OFF v2 payload, allergen tag mapping, nutrition extraction, the
absence of any price field, and graceful degradation on 404 / network error / timeout /
malformed payload (always ``None``, never a crash, never fabricated data).
"""

from __future__ import annotations

from decimal import Decimal

import httpx
import pytest

from cestaplan_api.adapters.openfoodfacts import OpenFoodFactsAdapter

# A realistic OFF v2 product payload. Note the deliberately-planted ``price`` / ``stores``
# keys: the adapter must ignore them (OFF is never a price source).
_VALID_PAYLOAD = {
    "code": "3017620422003",
    "status": 1,
    "status_verbose": "product found",
    "product": {
        "product_name": "Crema de avellanas",
        "brands": "MarcaX, SubMarca",
        "categories_tags": ["en:spreads", "en:hazelnut-spreads"],
        "ingredients_text": "Azúcar, aceite de palma, avellanas, leche, cacao",
        "allergens_tags": ["en:milk", "en:nuts", "en:soybeans"],
        "traces_tags": ["en:peanuts", "fr:gluten"],
        "nutriments": {
            "energy-kcal_100g": 539,
            "proteins_100g": 6.3,
            "carbohydrates_100g": 57.5,
            "sugars_100g": 56.3,
            "fat_100g": 30.9,
            "saturated-fat_100g": 10.6,
            "fiber_100g": 0,
            "salt_100g": 0.107,
        },
        "image_url": "https://images.openfoodfacts.org/front.jpg",
        # Planted noise that MUST NOT be read as a price:
        "price": "9.99",
        "stores": "SomeStore",
    },
}


def _adapter_with(handler) -> OpenFoodFactsAdapter:
    """Build an adapter whose HTTP calls are served by ``handler`` (no real network)."""
    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport)
    return OpenFoodFactsAdapter(client=client)


def _ok_handler(request: httpx.Request) -> httpx.Response:
    assert "openfoodfacts.org" in str(request.url)
    assert request.headers["User-Agent"].startswith("CestaPlan/")
    return httpx.Response(200, json=_VALID_PAYLOAD)


def test_valid_payload_parsed() -> None:
    off = _adapter_with(_ok_handler).fetch_by_barcode("3017620422003")
    assert off is not None
    assert off.barcode == "3017620422003"
    assert off.product_name == "Crema de avellanas"
    assert off.brands == "MarcaX, SubMarca"
    assert (off.ingredients_text or "").startswith("Azúcar")
    assert off.category_code == "hazelnut-spreads"  # most specific (last) category
    assert off.categories == ("spreads", "hazelnut-spreads")
    assert off.image_url == "https://images.openfoodfacts.org/front.jpg"
    assert off.source_url == "https://world.openfoodfacts.org/product/3017620422003"


def test_allergen_tag_mapping() -> None:
    off = _adapter_with(_ok_handler).fetch_by_barcode("3017620422003")
    assert off is not None
    # en:milk->milk, en:nuts->tree_nut, en:soybeans->soy (language prefix stripped + mapped).
    assert off.allergens == ("milk", "tree_nut", "soy")
    # traces: en:peanuts->peanut, fr:gluten->gluten (prefix-agnostic).
    assert off.traces == ("peanut", "gluten")


def test_nutrition_extracted_as_decimal() -> None:
    off = _adapter_with(_ok_handler).fetch_by_barcode("3017620422003")
    assert off is not None
    assert off.energy_kcal == Decimal("539")
    assert off.protein_g == Decimal("6.3")
    assert off.carbohydrate_g == Decimal("57.5")
    assert off.sugars_g == Decimal("56.3")
    assert off.fat_g == Decimal("30.9")
    assert off.saturated_fat_g == Decimal("10.6")
    assert off.fiber_g == Decimal("0")
    assert off.salt_g == Decimal("0.107")


def test_no_price_fields_anywhere() -> None:
    off = _adapter_with(_ok_handler).fetch_by_barcode("3017620422003")
    assert off is not None
    # The dataclass carries no price attribute...
    assert not hasattr(off, "price")
    assert not hasattr(off, "amount")
    # ...and the public serialization exposes nothing price-like despite the planted noise.
    import json

    blob = json.dumps(off.to_public_dict()).lower()
    for banned in ("price", "amount", "cost", "€", "eur", "9.99", "somestore"):
        assert banned not in blob


def test_missing_nutriment_is_none_never_zero() -> None:
    payload = {
        "status": 1,
        "product": {
            "product_name": "Sin datos nutricionales",
            "nutriments": {"proteins_100g": 5},  # only protein present
        },
    }

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    off = _adapter_with(handler).fetch_by_barcode("111")
    assert off is not None
    assert off.protein_g == Decimal("5")
    assert off.energy_kcal is None  # absent -> None, never fabricated 0
    assert off.fat_g is None


def test_404_returns_none() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"status": 0, "status_verbose": "product not found"})

    assert _adapter_with(handler).fetch_by_barcode("000") is None


def test_status_zero_returns_none() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": 0, "status_verbose": "product not found"})

    assert _adapter_with(handler).fetch_by_barcode("000") is None


def test_network_error_returns_none() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    assert _adapter_with(handler).fetch_by_barcode("123") is None


def test_timeout_returns_none() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    assert _adapter_with(handler).fetch_by_barcode("123") is None


def test_malformed_payload_returns_none() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=b"not-json{", headers={"content-type": "application/json"}
        )

    assert _adapter_with(handler).fetch_by_barcode("123") is None


def test_non_dict_payload_returns_none() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[1, 2, 3])

    assert _adapter_with(handler).fetch_by_barcode("123") is None


def test_blank_barcode_returns_none() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:  # pragma: no cover - not reached
        raise AssertionError("must not hit the network for a blank barcode")

    assert _adapter_with(handler).fetch_by_barcode("  ") is None


@pytest.mark.parametrize(
    ("tag", "code"),
    [
        ("en:gluten", "gluten"),
        ("en:eggs", "egg"),
        ("en:fish", "fish"),
        ("en:crustaceans", "crustacean"),
        ("en:molluscs", "mollusc"),
        ("en:celery", "celery"),
        ("en:mustard", "mustard"),
        ("en:sesame-seeds", "sesame"),
        ("en:sulphur-dioxide-and-sulphites", "sulphite"),
        ("en:lupin", "lupin"),
    ],
)
def test_full_allergen_vocabulary_mapping(tag: str, code: str) -> None:
    payload = {"status": 1, "product": {"allergens_tags": [tag]}}

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    off = _adapter_with(handler).fetch_by_barcode("1")
    assert off is not None
    assert off.allergens == (code,)
