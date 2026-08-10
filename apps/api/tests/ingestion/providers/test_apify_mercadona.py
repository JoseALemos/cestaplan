"""Apify Mercadona mapper + provider (igolaizola/mercadona-scraper) — offline, capture-anchored.

Driven by the sanitized 25-record capture (``tests/fixtures/providers/apify-mercadona/
sanitized.json``); no network is ever touched. Covered: the full 25-record map, an unknown
fingerprint blocking, structured net-content extraction (-> FIXED_PACKAGE costing), price + promo
(price_decreased -> regular=previous, promo=unit_price), the supplementary reference unit price,
barcode always None, net-content None when the unit is unknown, postal-code scope sealing, and the
Apify ``maxTotalChargeUsd`` billing cap on ``start_run``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from cestaplan_api.ingestion.contracts import PriceScope
from cestaplan_api.ingestion.providers.apify.client import ApifyClient
from cestaplan_api.ingestion.providers.apify.mapping import (
    ApifyMercadonaMapper,
    ApifyMercadonaRecord,
    InvalidMoneyValue,
    NonPositivePriceError,
    UnsupportedSchemaError,
    _decimal_from_str,
    _deepest_category_name,
    _reference_unit_price,
)
from cestaplan_api.ingestion.providers.contracts import (
    Availability,
    ContentUnit,
    SellUnit,
)
from cestaplan_api.ingestion.providers.mercadona.provider import MercadonaProvider
from cestaplan_api.ingestion.providers.quality import UNIT_PRICE_COSTED_PROVIDERS
from cestaplan_api.ingestion.providers.registry import registry
from cestaplan_api.models import (
    ExternalProduct,
    Ingredient,
    PriceObservation,
    Product,
    ProductVariant,
    ProviderIngredientMapping,
    Recipe,
    RecipeIngredient,
    Retailer,
)
from cestaplan_api.services.recipe_costing import cost_recipe
from cestaplan_api.services.store_zone_resolution import resolve_store_for_postal

_FIXTURE = (
    Path(__file__).parents[2] / "fixtures" / "providers" / "apify-mercadona" / "sanitized.json"
)
_POSTAL = "28001"
_NOW = datetime(2026, 8, 9, 10, 0, tzinfo=UTC)
# The 5L olive-oil garrafa (id 4241): unit_price 17.75, unit_size 5 l, reference 3.550 €/L.
_GARRAFA_ID = "4241"


def _records() -> list[dict]:
    return json.loads(_FIXTURE.read_text())


def _map(records: list[dict], *, postal_code: str | None = _POSTAL):
    return ApifyMercadonaMapper().map_products(
        records, postal_code=postal_code, observed_at=_NOW
    )


# --- mapper -------------------------------------------------------------- #
def test_maps_all_sample_records() -> None:
    products = _map(_records())
    assert len(products) == 25
    garrafa = next(p for p in products if p.external_product_id == _GARRAFA_ID)
    assert garrafa.provider == "apify-mercadona"
    assert garrafa.retailer_slug == "mercadona"
    assert garrafa.product_name == "Light olive oil Hacendado"
    assert garrafa.brand is None  # actor exposes no brand — never inferred
    assert garrafa.regular_price == Decimal("17.75")
    assert isinstance(garrafa.regular_price, Decimal)  # no float
    assert garrafa.promotional_price is None and garrafa.promotion is None
    assert garrafa.currency == "EUR"  # documented constant (Spain), not a source field
    assert garrafa.sell_unit is SellUnit.PACKAGE
    assert garrafa.variable_weight is False
    # Structured net content — the igolaizola advantage.
    assert garrafa.net_content_quantity == Decimal("5")
    assert garrafa.net_content_unit is ContentUnit.L
    # Supplementary €/L reference price.
    assert garrafa.unit_price == Decimal("3.550") and garrafa.unit_price_unit == "l"
    assert garrafa.availability is Availability.IN_STOCK
    assert garrafa.category == "Olive oil"  # deepest (most specific) category node
    assert garrafa.product_url is not None
    assert garrafa.product_url.endswith("/product/4241/aceite-oliva-04o-hacendado-garrafa")
    assert garrafa.image_url is not None
    assert garrafa.price_scope is PriceScope.POSTAL_CODE and garrafa.postal_code == _POSTAL


def test_kg_net_content_maps_to_kg_unit() -> None:
    # id 4908: a balsamic cream sold by kg (size_format "kg").
    cream = next(p for p in _map(_records()) if p.external_product_id == "4908")
    assert cream.net_content_quantity == Decimal("0.25")
    assert cream.net_content_unit is ContentUnit.KG
    assert cream.unit_price == Decimal("7.200") and cream.unit_price_unit == "kg"


def test_barcode_always_none() -> None:
    assert all(p.barcode is None for p in _map(_records()))  # no EAN in this actor — never invented


def test_observed_at_is_the_threaded_retrieval_time() -> None:
    # This actor emits no source timestamp; observed_at is the retrieval time threaded in.
    products = _map(_records())
    assert all(p.observed_at == _NOW for p in products)
    assert all(p.observed_at.tzinfo is not None for p in products)


def test_postal_scope_set_with_and_without_postal() -> None:
    with_postal = _map(_records())
    assert all(p.price_scope is PriceScope.POSTAL_CODE for p in with_postal)
    assert all(p.postal_code == _POSTAL for p in with_postal)
    # No postal code -> the price cannot be localised, so the scope is UNKNOWN (honest limit).
    without = _map(_records(), postal_code=None)
    assert all(p.price_scope is PriceScope.UNKNOWN for p in without)
    assert all(p.postal_code is None for p in without)


def test_empty_response_maps_to_nothing() -> None:
    assert _map([]) == []


def test_unknown_fingerprint_blocks_normalization() -> None:
    drifted = _records()
    # Type change on a core field (unit_size number -> string) -> different core -> blocks.
    drifted[0]["price_instructions"]["unit_size"] = "5"
    with pytest.raises(UnsupportedSchemaError):
        _map(drifted)


def test_int_float_unit_size_variance_does_not_shift_fingerprint() -> None:
    # unit_size is int for some products, float for others; that harmless variance must never block.
    records = _records()
    for r in records:  # force every unit_size to a float
        r["price_instructions"]["unit_size"] = float(r["price_instructions"]["unit_size"])
    assert len(_map(records)) == 25  # still recognised, still maps
    for r in records:  # and every unit_size to an int-valued float structure via ints where whole
        r["price_instructions"]["unit_size"] = 1
    assert len(_map(records)) == 25


def test_missing_optional_fields_do_not_block_batch() -> None:
    # thumbnail/categories/unavailable_* are non-core: a capture lacking them still maps and the
    # fingerprint is unchanged.
    records = _records()
    for key in ("thumbnail", "categories", "unavailable_from", "unavailable_weekdays"):
        records[0].pop(key, None)
    products = _map(records)
    assert len(products) == 25
    first = next(p for p in products if p.external_product_id == _GARRAFA_ID)
    assert first.image_url is None and first.category is None
    assert first.availability is Availability.IN_STOCK  # published, no unavailability window


# --- promotion (price_decreased) ----------------------------------------- #
def test_price_decreased_reads_previous_as_regular_and_unit_price_as_promo() -> None:
    records = _records()
    pi = records[0]["price_instructions"]  # garrafa: unit_price 17.75, previous 18.75
    pi["price_decreased"] = True
    garrafa = next(p for p in _map(records) if p.external_product_id == _GARRAFA_ID)
    assert garrafa.regular_price == Decimal("18.75")  # previous_unit_price (with leading spaces)
    assert garrafa.promotional_price == Decimal("17.75")  # the shelf price paid
    assert garrafa.promotion is not None
    assert garrafa.promotion.promotional_price == Decimal("17.75")
    assert garrafa.promotion.percentage_discount == Decimal("5.33")


def test_price_decreased_without_higher_previous_is_not_a_markdown() -> None:
    records = _records()
    pi = records[0]["price_instructions"]
    pi["price_decreased"] = True
    pi["previous_unit_price"] = "10.00"  # BELOW unit_price 17.75 -> not a genuine markdown
    garrafa = next(p for p in _map(records) if p.external_product_id == _GARRAFA_ID)
    assert garrafa.regular_price == Decimal("17.75")
    assert garrafa.promotional_price is None and garrafa.promotion is None


# --- net content edge cases ---------------------------------------------- #
def test_unknown_size_format_yields_no_net_content() -> None:
    records = _records()
    records[0]["price_instructions"]["size_format"] = "botella"  # unknown unit -> never guessed
    garrafa = next(p for p in _map(records) if p.external_product_id == _GARRAFA_ID)
    assert garrafa.net_content_quantity is None and garrafa.net_content_unit is None


def test_every_sampled_item_passes_the_net_content_consistency_invariant() -> None:
    # The guard must not false-trip: unit_price / unit_size == reference_price on all 25 real items,
    # so every product keeps its structured net content.
    assert all(p.net_content_quantity is not None for p in _map(_records()))
    assert all(p.net_content_unit is not None for p in _map(_records()))


def test_pack_inconsistent_net_content_is_dropped_not_undercounted() -> None:
    # A pack: unit_size is a SINGLE unit (1 L) but reference_price is computed over total_units (6),
    # so unit_price / unit_size (6.00) disagrees with reference_price (1.00). Costing by unit_size
    # would undercount the pack 6x -> the guard drops net content so it is simply not costed.
    records = _records()
    pi = records[0]["price_instructions"]
    pi.update(
        {
            "is_pack": True,
            "unit_size": 1,
            "total_units": 6,
            "unit_price": "6.00",  # pack shelf price
            "reference_price": "1.00",  # €/L over the 6 L total
            "reference_format": "L",
            "size_format": "l",
        }
    )
    pack = next(p for p in _map(records) if p.external_product_id == _GARRAFA_ID)
    assert pack.net_content_quantity is None and pack.net_content_unit is None
    assert pack.regular_price == Decimal("6.00")  # price still mapped; just not net-content costed


def test_approx_size_yields_no_net_content() -> None:
    # Sold by an approximate/variable measure -> unit_size is not an exact fixed content.
    records = _records()
    records[0]["price_instructions"]["approx_size"] = True
    garrafa = next(p for p in _map(records) if p.external_product_id == _GARRAFA_ID)
    assert garrafa.net_content_quantity is None and garrafa.net_content_unit is None


@pytest.mark.parametrize("bad_price", ["0", "-1", "0.00"])
def test_non_positive_price_is_refused_never_zero_or_negative_cost(bad_price: str) -> None:
    records = _records()
    records[0]["price_instructions"]["unit_price"] = bad_price
    with pytest.raises(NonPositivePriceError):
        _map(records)


def test_deepest_category_name() -> None:
    assert _deepest_category_name(None) is None
    assert _deepest_category_name([]) is None
    tree = [{"name": "Top", "level": 0, "categories": [{"name": "Leaf", "level": 1}]}]
    assert _deepest_category_name(tree) == "Leaf"


# --- reference unit price parsing ---------------------------------------- #
@pytest.mark.parametrize(
    ("price", "fmt", "expected"),
    [
        ("3.550", "L", (Decimal("3.550"), "l")),
        ("7.200", "kg", (Decimal("7.200"), "kg")),
        ("2,50", "L", (Decimal("2.50"), "l")),  # comma decimal tolerated
        ("1.00", "caja", (None, None)),  # unknown unit -> never guessed
        ("nan", "L", (None, None)),  # unparseable price
        ("0", "L", (None, None)),  # non-positive
        ("3.55", None, (None, None)),  # no format
    ],
)
def test_reference_unit_price(price, fmt, expected) -> None:
    assert _reference_unit_price(price, fmt) == expected


# --- registry + policy guard --------------------------------------------- #
def test_provider_registered_and_not_unit_price_costed() -> None:
    assert registry.has("apify-mercadona")
    provider = registry.get("apify-mercadona")
    assert isinstance(provider, MercadonaProvider)
    meta = provider.get_source_metadata()
    assert meta.retailer_slug == "mercadona"
    assert meta.official is False
    # It now carries structured net content, so it is costed as a normal FIXED_PACKAGE — NOT via the
    # unit-price workaround.
    assert "apify-mercadona" not in UNIT_PRICE_COSTED_PROVIDERS


# --- money parsing ------------------------------------------------------- #
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("17.75", Decimal("17.75")),
        ("       18.75", Decimal("18.75")),  # leading whitespace the actor emits
        ("5,04", Decimal("5.04")),
        ("5,04 €", Decimal("5.04")),
        ("1.234,56", Decimal("1234.56")),
    ],
)
def test_money_string_parsing(raw: str, expected: Decimal) -> None:
    assert _decimal_from_str(raw) == expected


@pytest.mark.parametrize("raw", ["", "abc", "1.2.3", "--5"])
def test_money_string_invalid_raises_typed_error(raw: str) -> None:
    with pytest.raises(InvalidMoneyValue):
        _decimal_from_str(raw)


def test_string_unit_size_number_parses_via_record() -> None:
    # A string unit_size is a different core type (blocks at the fingerprint gate), so exercise the
    # Decimal coercion through a direct record validation.
    records = _records()
    records[0]["price_instructions"]["unit_size"] = "5"
    rec = ApifyMercadonaRecord.model_validate(records[0])
    assert rec.price_instructions.unit_size == Decimal("5")


# --- transport: Apify billing cap ---------------------------------------- #
def test_start_run_includes_max_total_charge_usd_query_param() -> None:
    captured: dict[str, httpx.URL] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = request.url
        return httpx.Response(201, json={"data": {"id": "run-1", "status": "RUNNING"}})

    client = ApifyClient(
        api_token="APIFY-TOKEN", client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    run_id = client.start_run(
        "igolaizola~mercadona-scraper", {"maxItems": 5}, max_total_charge_usd=2.0
    )
    assert run_id == "run-1"
    assert captured["url"].params.get("maxTotalChargeUsd") == "2.0"


def test_start_run_omits_cap_when_not_positive() -> None:
    captured: dict[str, httpx.URL] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = request.url
        return httpx.Response(201, json={"data": {"id": "r", "status": "RUNNING"}})

    client = ApifyClient(
        api_token="T", client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    client.start_run("actor~x", {"maxItems": 1})  # no cap passed
    assert "maxTotalChargeUsd" not in captured["url"].params


# --- FIXED_PACKAGE recipe costing (DB, no network) ----------------------- #
_LECHE = "leche_entera"


def _ing(db: Session, name: str) -> int:
    return db.execute(select(Ingredient.id).where(Ingredient.canonical_name == name)).scalar_one()


def _mercadona_retailer(db: Session) -> int:
    rid = db.execute(
        select(Retailer.id).where(Retailer.slug == "mercadona")
    ).scalar_one_or_none()
    if rid is not None:
        return rid
    r = Retailer(
        slug="mercadona", name="Mercadona", adapter_key="apify-mercadona", is_synthetic=True
    )
    db.add(r)
    db.flush()
    return r.id


def _add_garrafa(db: Session, rid: int, store_id: int) -> None:
    """A Mercadona 5 L oil garrafa: PACKAGE price 17.75 €, structured net content 5 L (the mapper's
    shape). Costed as FIXED_PACKAGE, stamped with the delivery-zone store (postal_code scope)."""
    product = Product(name="Aceite oliva Hacendado garrafa 5L", is_synthetic=False)
    db.add(product)
    db.flush()
    external = ExternalProduct(retailer_id=rid, external_id=_GARRAFA_ID)
    db.add(external)
    db.flush()
    variant = ProductVariant(
        retailer_id=rid,
        external_product_id=external.id,
        product_id=product.id,
        display_name="Aceite oliva Hacendado garrafa 5L",
        sell_unit="package",
        variable_weight=False,
        net_content_quantity=Decimal("5"),  # structured net content -> FIXED_PACKAGE
        net_content_unit="l",
        unit_price=Decimal("3.55"),  # supplementary reference; not used to cost a fixed package
        unit_price_unit="l",
    )
    db.add(variant)
    db.flush()
    db.add(
        PriceObservation(
            retailer_id=rid,
            store_id=store_id,
            product_variant_id=variant.id,
            price_scope="postal_code",
            price_type="regular",
            amount=Decimal("17.75"),  # the PACKAGE price
            currency="EUR",
            observed_at=_NOW,
            imported_at=_NOW,
            valid_from=_NOW,
            confidence_score=Decimal("1.0"),
            staging_only=True,
        )
    )
    db.add(
        ProviderIngredientMapping(
            provider_code="apify-mercadona",
            ingredient_id=_ing(db, _LECHE),
            canonical_ingredient_key="leche_entera",
            retailer_slug="mercadona",
            external_product_id=_GARRAFA_ID,
            normalized_product_id=product.id,
            mapping_status="auto_approved",
            mapping_method="exact_alias",
            confidence_score=Decimal("0.96"),
            unit_compatibility="compatible",
            required_review=False,
            active=True,
        )
    )
    db.flush()


def _recipe(db: Session, qty: str, unit: str) -> Recipe:
    recipe = Recipe(origin="seed", title="Test Recipe", servings=1, is_synthetic=True)
    db.add(recipe)
    db.flush()
    db.add(
        RecipeIngredient(
            recipe_id=recipe.id,
            ingredient_id=_ing(db, _LECHE),
            canonical_name="leche_entera",
            display_name="Aceite",
            quantity=Decimal(qty),
            unit=unit,
            optional=False,
        )
    )
    db.flush()
    db.refresh(recipe)
    return recipe


def test_mercadona_recipe_costs_as_fixed_package_by_net_content(db_session: Session) -> None:
    # The garrafa carries structured net content (5 L), so it is costed as a FIXED_PACKAGE: a
    # 200 ml requirement buys ONE whole 5 L garrafa (17.75 €) — whole packages, never fractional.
    rid = _mercadona_retailer(db_session)
    store = resolve_store_for_postal(db_session, rid, "28001")
    _add_garrafa(db_session, rid, store.id)
    recipe = _recipe(db_session, "200", "ml")
    result = cost_recipe(db_session, recipe, "apify-mercadona", store_id=store.id)
    assert result.fully_costable is True
    line = next(line for line in result.lines if line.canonical_name == "leche_entera")
    assert line.costing_mode == "fixed_package"
    assert line.line_cost == Decimal("17.75")  # one whole 5 L garrafa
    assert line.package_quantity == Decimal("5") and line.package_unit == "l"


def test_mercadona_recipe_costs_whole_multiple_of_net_content(db_session: Session) -> None:
    # 12 L required -> ceil(12/5) = 3 garrafas -> 3 x 17.75 = 53.25 €.
    rid = _mercadona_retailer(db_session)
    store = resolve_store_for_postal(db_session, rid, "28001")
    _add_garrafa(db_session, rid, store.id)
    recipe = _recipe(db_session, "12", "l")
    result = cost_recipe(db_session, recipe, "apify-mercadona", store_id=store.id)
    line = next(line for line in result.lines if line.canonical_name == "leche_entera")
    assert line.costing_mode == "fixed_package"
    assert line.line_cost == Decimal("53.25")


def test_mercadona_zone_price_never_crosses_to_another_zone(db_session: Session) -> None:
    # HARD INVARIANT: a price captured in one delivery zone is NEVER served to a plan of another
    # zone, nor to a no-zone (national) plan — the recipe is simply not costed there.
    rid = _mercadona_retailer(db_session)
    zone_cordoba = resolve_store_for_postal(db_session, rid, "14006")
    zone_madrid = resolve_store_for_postal(db_session, rid, "28001")
    _add_garrafa(db_session, rid, zone_cordoba.id)  # price captured only in 14006
    recipe = _recipe(db_session, "200", "ml")

    same = cost_recipe(db_session, recipe, "apify-mercadona", store_id=zone_cordoba.id)
    assert same.fully_costable is True

    other = cost_recipe(db_session, recipe, "apify-mercadona", store_id=zone_madrid.id)
    assert other.fully_costable is False

    national = cost_recipe(db_session, recipe, "apify-mercadona", store_id=None)
    assert national.fully_costable is False


def test_zone_store_resolution_is_idempotent(db_session: Session) -> None:
    rid = _mercadona_retailer(db_session)
    first = resolve_store_for_postal(db_session, rid, "14006")
    second = resolve_store_for_postal(db_session, rid, "14006")
    assert first.id == second.id  # a second resolution never duplicates the zone store
    assert first.postal_code == "14006" and first.retailer_id == rid
