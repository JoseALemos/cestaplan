"""Apify Mercadona mapper + provider (spec §5-§8) — offline, anchored to the real capture.

The mapper is driven by the sanitized 5-record capture (``tests/fixtures/providers/
apify-mercadona/sanitized.json``); no network is ever touched. Covered: the full 5-record map,
an unknown fingerprint blocking, ``unitPrice`` parsing (valid + invalid), ``ean`` null -> barcode
None, ``scrapedAt`` -> tz-aware observed_at, postal-code scope, and unit-price recipe costing.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from cestaplan_api.ingestion.contracts import PriceScope
from cestaplan_api.ingestion.providers.apify.mapping import (
    ApifyMercadonaMapper,
    ApifyMercadonaProvider,
    UnsupportedSchemaError,
    _parse_unit_price,
)
from cestaplan_api.ingestion.providers.contracts import Availability, SellUnit
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


def _records() -> list[dict]:
    return json.loads(_FIXTURE.read_text())


# --- mapper -------------------------------------------------------------- #
def test_maps_all_five_sample_records() -> None:
    products = ApifyMercadonaMapper().map_products(_records(), postal_code=_POSTAL)
    assert len(products) == 5
    first = next(p for p in products if p.external_product_id == "10381")
    assert first.provider == "apify-mercadona"
    assert first.retailer_slug == "mercadona"
    assert first.product_name == "Leche semidesnatada Hacendado"
    assert first.brand == "Hacendado"
    assert first.regular_price == Decimal("5.04")
    assert isinstance(first.regular_price, Decimal)  # no float
    assert first.currency == "EUR"
    assert first.sell_unit is SellUnit.PACKAGE
    assert first.variable_weight is False
    assert first.net_content_quantity is None and first.net_content_unit is None  # §7
    assert first.unit_price == Decimal("0.84") and first.unit_price_unit == "l"
    assert first.availability is Availability.IN_STOCK
    assert first.category == "Huevos, leche y mantequilla"
    assert first.promotional_price is None and first.promotion is None


def test_ean_null_maps_to_no_barcode() -> None:
    products = ApifyMercadonaMapper().map_products(_records(), postal_code=_POSTAL)
    assert all(p.barcode is None for p in products)  # ean is null in the capture — never invented


def test_observed_at_is_tz_aware_from_scraped_at() -> None:
    first = next(
        p
        for p in ApifyMercadonaMapper().map_products(_records(), postal_code=_POSTAL)
        if p.external_product_id == "10381"
    )
    assert first.observed_at == datetime(2026, 8, 8, 22, 12, 20, 907000, tzinfo=UTC)
    assert first.observed_at.tzinfo is not None


def test_postal_scope_set_with_and_without_postal() -> None:
    with_postal = ApifyMercadonaMapper().map_products(_records(), postal_code=_POSTAL)
    assert all(p.price_scope is PriceScope.POSTAL_CODE for p in with_postal)
    assert all(p.postal_code == _POSTAL for p in with_postal)
    # No postal code -> the price cannot be localised, so the scope is UNKNOWN (honest limit).
    without = ApifyMercadonaMapper().map_products(_records(), postal_code=None)
    assert all(p.price_scope is PriceScope.UNKNOWN for p in without)
    assert all(p.postal_code is None for p in without)


def test_empty_response_maps_to_nothing() -> None:
    assert ApifyMercadonaMapper().map_products([], postal_code=_POSTAL) == []


def test_unknown_fingerprint_blocks_normalization() -> None:
    drifted = _records()
    drifted[0]["price"] = "5.04"  # type change float->string on a core field -> different core
    with pytest.raises(UnsupportedSchemaError):
        ApifyMercadonaMapper().map_products(drifted, postal_code=_POSTAL)


def test_populated_ean_and_promo_still_match_core_fingerprint() -> None:
    # ean/promotionPrice are excluded from the pinned core, so real populated data maps fine.
    records = _records()
    for r in records:
        r["ean"] = "8410000000000"
        r["promotionPrice"] = round(r["price"] * 0.9, 2)
    products = ApifyMercadonaMapper().map_products(records, postal_code=_POSTAL)
    assert all(p.barcode == "8410000000000" for p in products)
    assert all(p.promotional_price is not None for p in products)
    assert all(p.promotion is not None for p in products)


# --- unitPrice parsing --------------------------------------------------- #
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("0.84/L", (Decimal("0.84"), "l")),
        ("1.20/kg", (Decimal("1.20"), "kg")),
        ("2,50/L", (Decimal("2.50"), "l")),  # comma decimal tolerated
        ("0.99/ML", (Decimal("0.99"), "ml")),
        ("caja de 6", (None, None)),  # no "/" separator
        ("/L", (None, None)),  # missing value
        ("0.84/caja", (None, None)),  # unknown unit -> never guessed
        ("0/L", (None, None)),  # non-positive value
    ],
)
def test_parse_unit_price(raw: str, expected: tuple[Decimal | None, str | None]) -> None:
    assert _parse_unit_price(raw) == expected


# --- registry guard ------------------------------------------------------ #
def test_provider_registered_and_resolvable() -> None:
    assert registry.has("apify-mercadona")
    provider = registry.get("apify-mercadona")
    assert isinstance(provider, ApifyMercadonaProvider)
    meta = provider.get_source_metadata()
    assert meta.retailer_slug == "mercadona"
    assert meta.official is False
    assert "apify-mercadona" in UNIT_PRICE_COSTED_PROVIDERS


# --- unit-price recipe costing (DB, no network) -------------------------- #
_NOW = datetime.now(UTC)
_LECHE = "leche_entera"


def _ing(db: Session, name: str) -> int:
    return db.execute(select(Ingredient.id).where(Ingredient.canonical_name == name)).scalar_one()


def _mercadona_retailer(db: Session) -> int:
    """Get-or-create ``mercadona`` (cost_recipe maps apify-mercadona -> slug 'mercadona')."""
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


def _add_mercadona_leche(db: Session, rid: int, store_id: int) -> None:
    """A Mercadona 6x1L milk: observation stores the PACKAGE price (5.04 €), the variant carries
    the real 0.84 €/l unit price — exactly the mapper's output shape (net content None). The price
    is stamped with the delivery-zone ``store_id`` (postal_code scope)."""
    product = Product(name="Leche semidesnatada Hacendado", is_synthetic=False)
    db.add(product)
    db.flush()
    external = ExternalProduct(retailer_id=rid, external_id="10381")
    db.add(external)
    db.flush()
    variant = ProductVariant(
        retailer_id=rid,
        external_product_id=external.id,
        product_id=product.id,
        display_name="Leche semidesnatada Hacendado",
        sell_unit="package",
        variable_weight=False,
        net_content_quantity=None,  # Mercadona capture exposes no net content
        net_content_unit=None,
        unit_price=Decimal("0.84"),
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
            amount=Decimal("5.04"),  # the PACKAGE price, NOT the €/l
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
            external_product_id="10381",
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
            display_name="Leche entera",
            quantity=Decimal(qty),
            unit=unit,
            optional=False,
        )
    )
    db.flush()
    db.refresh(recipe)
    return recipe


def test_mercadona_recipe_costs_by_unit_price(db_session: Session) -> None:
    # 200 ml must cost from the 0.84 €/l unit price (0.168 -> 0.17 €), NOT the 5.04 € pack price
    # (which would give a 6x over-cost). This exercises the UNIT_PRICE_COSTED_PROVIDERS path. The
    # Mercadona price is zonified, so the plan must carry the delivery-zone store.
    rid = _mercadona_retailer(db_session)
    store = resolve_store_for_postal(db_session, rid, "28001")
    _add_mercadona_leche(db_session, rid, store.id)
    recipe = _recipe(db_session, "200", "ml")
    result = cost_recipe(db_session, recipe, "apify-mercadona", store_id=store.id)
    assert result.fully_costable is True
    leche = next(line for line in result.lines if line.canonical_name == "leche_entera")
    assert leche.costing_mode == "variable_volume"
    assert leche.line_cost == Decimal("0.17")


def test_mercadona_zone_price_never_crosses_to_another_zone(db_session: Session) -> None:
    # HARD INVARIANT: a price captured in one delivery zone is NEVER served to a plan of another
    # zone, nor to a no-zone (national) plan. In those cases the recipe is simply NOT costed —
    # never costed with the wrong zone's price.
    rid = _mercadona_retailer(db_session)
    zone_cordoba = resolve_store_for_postal(db_session, rid, "14006")
    zone_madrid = resolve_store_for_postal(db_session, rid, "28001")
    _add_mercadona_leche(db_session, rid, zone_cordoba.id)  # price captured only in 14006
    recipe = _recipe(db_session, "200", "ml")

    # Same zone -> costed correctly.
    same = cost_recipe(db_session, recipe, "apify-mercadona", store_id=zone_cordoba.id)
    assert same.fully_costable is True

    # Different zone -> no price (store_id value-match yields nothing), never the 14006 price.
    other = cost_recipe(db_session, recipe, "apify-mercadona", store_id=zone_madrid.id)
    assert other.fully_costable is False

    # No zone (national plan) -> a zonified price never satisfies a national requirement.
    national = cost_recipe(db_session, recipe, "apify-mercadona", store_id=None)
    assert national.fully_costable is False


def test_zone_store_resolution_is_idempotent(db_session: Session) -> None:
    rid = _mercadona_retailer(db_session)
    first = resolve_store_for_postal(db_session, rid, "14006")
    second = resolve_store_for_postal(db_session, rid, "14006")
    assert first.id == second.id  # a second resolution never duplicates the zone store
    assert first.postal_code == "14006" and first.retailer_id == rid


def test_capture_missing_optional_fields_does_not_block_batch() -> None:
    # A real, heterogeneous capture where a product lacks unitPrice/brand/category/imageUrl must
    # still map — these nullable, non-core fields never block the batch nor change the fingerprint.
    records = _records()
    records[0].pop("unitPrice")
    records[0].pop("brand")
    records[0].pop("category")
    records[0].pop("imageUrl")
    products = ApifyMercadonaMapper().map_products(records, postal_code=_POSTAL)
    assert len(products) == 5
    first = next(p for p in products if p.external_product_id == "10381")
    assert first.unit_price is None and first.unit_price_unit is None
    assert first.brand is None and first.category is None and first.image_url is None
    assert first.regular_price == Decimal("5.04")  # core field still parsed


def test_price_string_with_comma_decimal_is_parsed() -> None:
    records = _records()
    records[0]["price"] = "5,04"  # a comma-decimal string price is normalised by Money
    # A string price is a different core type -> fingerprint drift is expected to block; assert the
    # Money parser itself normalises the comma via a direct record validation instead.
    from cestaplan_api.ingestion.providers.apify.mapping import ApifyMercadonaRecord

    rec = ApifyMercadonaRecord.model_validate(records[0])
    assert rec.price == Decimal("5.04")
