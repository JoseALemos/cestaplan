"""RecipeCostingValidationReport (audit §6) — DB-backed, no network."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from cestaplan_api.models import (
    ExternalProduct,
    PriceObservation,
    Product,
    ProductVariant,
    ProviderIngredientMapping,
    Recipe,
    RecipeIngredient,
    Retailer,
)
from cestaplan_api.services.recipe_costing_validation import validate_recipe_costing

_NOW = datetime.now(UTC)
_PROV = "test-validate-prov"
_AVENA, _LECHE, _PLATANO = 792, 779, 760


def _product(
    db: Session,
    rid: int,
    ing_id: int,
    key: str,
    name: str,
    *,
    net_qty: str | None,
    net_unit: str | None,
    price: str,
    unit_price: str | None = None,
    unit_price_unit: str | None = None,
    variable_weight: bool = False,
) -> None:
    product = Product(name=name, is_synthetic=False)
    db.add(product)
    db.flush()
    ext = ExternalProduct(retailer_id=rid, external_id=f"EXT-{name[:16]}")
    db.add(ext)
    db.flush()
    v = ProductVariant(
        retailer_id=rid,
        external_product_id=ext.id,
        product_id=product.id,
        display_name=name,
        sell_unit="package",
        variable_weight=variable_weight,
        net_content_quantity=Decimal(net_qty) if net_qty is not None else None,
        net_content_unit=net_unit,
        unit_price=Decimal(unit_price) if unit_price is not None else None,
        unit_price_unit=unit_price_unit,
    )
    db.add(v)
    db.flush()
    db.add(
        PriceObservation(
            retailer_id=rid,
            product_variant_id=v.id,
            price_scope="national",
            price_type="regular",
            amount=Decimal(price),
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
            provider_code=_PROV,
            ingredient_id=ing_id,
            canonical_ingredient_key=key,
            retailer_slug=_PROV,
            external_product_id=ext.external_id,
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


def _recipe(db: Session) -> Recipe:
    recipe = Recipe(origin="seed", title="Validate Recipe", servings=2, is_synthetic=True)
    db.add(recipe)
    db.flush()
    for ing_id, key, qty, unit in [
        (_AVENA, "avena_copos", "80", "g"),
        (_LECHE, "leche_entera", "400", "ml"),
        (_PLATANO, "platano", "160", "g"),
    ]:
        db.add(
            RecipeIngredient(
                recipe_id=recipe.id,
                ingredient_id=ing_id,
                canonical_name=key,
                display_name=key,
                quantity=Decimal(qty),
                unit=unit,
                optional=False,
            )
        )
    db.flush()
    db.refresh(recipe)
    return recipe


def _retailer(db: Session) -> int:
    r = Retailer(slug=_PROV, name="Validate Prov", adapter_key="test", is_synthetic=True)
    db.add(r)
    db.flush()
    return r.id


def test_report_is_fully_costable_and_comparison_eligible(db_session: Session) -> None:
    rid = _retailer(db_session)
    _product(
        db_session,
        rid,
        _AVENA,
        "avena_copos",
        "Copos 500g",
        net_qty="500",
        net_unit="g",
        price="0.75",
    )
    _product(
        db_session,
        rid,
        _LECHE,
        "leche_entera",
        "Leche 1L",
        net_qty="1000",
        net_unit="ml",
        price="0.95",
    )
    _product(
        db_session,
        rid,
        _PLATANO,
        "platano",
        "Plátano bandeja 700 g",
        net_qty="700",
        net_unit="g",
        price="2.49",
        unit_price="3.56",
        unit_price_unit="kg",
    )
    recipe = _recipe(db_session)

    rep = validate_recipe_costing(db_session, recipe, _PROV)

    assert rep.fully_costable is True
    assert rep.comparison_eligible is True
    assert rep.mandatory_ingredients == 3
    assert rep.resolved_products == 3
    assert rep.unresolved_products == 0
    assert rep.costing_modes == {"fixed_package": 3}
    assert rep.scope_compatible is True
    assert rep.price_freshness_valid is True
    assert rep.blockers == []
    assert rep.input_fingerprint


def test_report_blocks_on_approximate_weight_without_rules(db_session: Session) -> None:
    rid = _retailer(db_session)
    _product(
        db_session,
        rid,
        _AVENA,
        "avena_copos",
        "Copos 500g",
        net_qty="500",
        net_unit="g",
        price="0.75",
    )
    _product(
        db_session,
        rid,
        _LECHE,
        "leche_entera",
        "Leche 1L",
        net_qty="1000",
        net_unit="ml",
        price="0.95",
    )
    # Plátano with NO net content but a reference €/kg -> approximate weight, not costable.
    _product(
        db_session,
        rid,
        _PLATANO,
        "platano",
        "Plátano al peso",
        net_qty=None,
        net_unit=None,
        price="2.84",
        unit_price="2.90",
        unit_price_unit="kg",
    )
    recipe = _recipe(db_session)

    rep = validate_recipe_costing(db_session, recipe, _PROV)

    assert rep.fully_costable is False
    assert rep.comparison_eligible is False
    assert rep.unresolved_products == 1
    assert "approximate_weight_without_rules" in rep.blockers


def test_validation_writes_no_production_data(db_session: Session) -> None:
    rid = _retailer(db_session)
    _product(
        db_session,
        rid,
        _AVENA,
        "avena_copos",
        "Copos 500g",
        net_qty="500",
        net_unit="g",
        price="0.75",
    )
    recipe = _recipe(db_session)
    validate_recipe_costing(db_session, recipe, _PROV)
    # The report is read-only: no production (non-staging) observation exists for this retailer.
    non_staging = db_session.execute(
        select(func.count())
        .select_from(PriceObservation)
        .join(ProductVariant, ProductVariant.id == PriceObservation.product_variant_id)
        .where(ProductVariant.retailer_id == rid, PriceObservation.staging_only.is_(False))
    ).scalar()
    assert non_staging == 0
