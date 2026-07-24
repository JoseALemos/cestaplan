"""Recipe coverage (§Z), shadow mode (§AA) and readiness (§AC) — DB-backed, no network.

The critical invariant threaded through every test: staging/shadow data NEVER contaminates a
production price/plan, and readiness NEVER changes activation state automatically.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from cestaplan_api.ingestion.current_price import CurrentPriceService
from cestaplan_api.models import (
    ExternalProduct,
    Ingredient,
    PriceObservation,
    Product,
    ProductVariant,
    ProviderActivation,
    ProviderIngredientMapping,
    Retailer,
    ShadowEvaluationRun,
)
from cestaplan_api.services.production_readiness import evaluate_production_readiness
from cestaplan_api.services.provider_shadow import run_provider_shadow
from cestaplan_api.services.recipe_catalog_coverage import evaluate_recipe_catalog_coverage

_NOW = datetime.now(UTC)


def _obs(variant: ProductVariant, *, amount: str, staging: bool) -> PriceObservation:
    return PriceObservation(
        retailer_id=variant.retailer_id,
        product_variant_id=variant.id,
        price_scope="national",
        price_type="regular",
        amount=Decimal(amount),
        currency="EUR",
        observed_at=_NOW,
        imported_at=_NOW,
        valid_from=_NOW,
        confidence_score=Decimal("1.0"),
        staging_only=staging,
    )


def test_staging_price_is_excluded_from_production(
    db_session: Session, variant: ProductVariant
) -> None:
    db_session.add(_obs(variant, amount="1.99", staging=True))
    db_session.flush()
    prices = CurrentPriceService()
    # Production view never sees the staging row; the staging view does.
    assert prices.current(db_session, variant.id, as_of=_NOW, staging=False) is None
    staged = prices.current(db_session, variant.id, as_of=_NOW, staging=True)
    assert staged is not None and staged.amount == Decimal("1.9900")


def test_production_price_is_unaffected_by_a_staging_row(
    db_session: Session, variant: ProductVariant
) -> None:
    db_session.add(_obs(variant, amount="2.50", staging=False))  # real production price
    db_session.add(_obs(variant, amount="9.99", staging=True))  # shadow/staging noise
    db_session.flush()
    prod = CurrentPriceService().current(db_session, variant.id, as_of=_NOW, staging=False)
    assert prod is not None and prod.amount == Decimal("2.5000")  # staging never overrides it


def test_active_provider_mapping_unblocks_an_ingredient(db_session: Session) -> None:
    ing = db_session.execute(
        select(Ingredient).where(Ingredient.canonical_name == "aceite_oliva")
    ).scalar_one()
    rid = db_session.execute(select(Retailer.id).where(Retailer.slug == "alcampo")).scalar_one()
    product = Product(name="AOVE test 1L", is_synthetic=False)
    db_session.add(product)
    db_session.flush()
    ext = ExternalProduct(retailer_id=rid, external_id="TEST-AOVE-COV")
    db_session.add(ext)
    db_session.flush()
    var = ProductVariant(
        retailer_id=rid,
        external_product_id=ext.id,
        product_id=product.id,
        display_name="Aceite de oliva 1 L",
        sell_unit="package",
        net_content_quantity=Decimal("1000"),
        net_content_unit="ml",
    )
    db_session.add(var)
    db_session.flush()
    db_session.add(_obs(var, amount="5.20", staging=True))
    db_session.add(
        ProviderIngredientMapping(
            provider_code="parsebot-alcampo",
            ingredient_id=ing.id,
            canonical_ingredient_key="aceite_oliva",
            retailer_slug="alcampo",
            external_product_id="TEST-AOVE-COV",
            normalized_product_id=product.id,
            mapping_status="auto_approved",
            mapping_method="exact_alias",
            confidence_score=Decimal("0.96"),
            unit_compatibility="compatible",
            required_review=False,
            active=True,
        )
    )
    db_session.flush()
    cov = evaluate_recipe_catalog_coverage(
        db_session, "parsebot-alcampo", scope="staging", recipe_limit=20
    )
    assert cov.mapped_ingredients >= 1  # the approved mapping is honoured
    assert cov.fully_costable_recipes == 0  # single-word ingredients still need review


def test_coverage_is_honestly_low_for_a_small_sample(db_session: Session) -> None:
    cov = evaluate_recipe_catalog_coverage(
        db_session, "parsebot-alcampo", scope="staging", recipe_limit=10
    )
    assert cov.total_recipes == 10
    assert cov.costing_coverage < Decimal("0.5")  # a ten-product sample never costs recipes
    assert cov.priority_unmapped_ingredients  # produces an actionable priority list
    assert cov.next_steps


def test_shadow_run_persists_and_never_activates_production(db_session: Session) -> None:
    before = db_session.execute(
        select(ProviderActivation.production_approved_at).where(
            ProviderActivation.provider_code == "parsebot-alcampo"
        )
    ).scalar_one_or_none()
    run = run_provider_shadow(db_session, "parsebot-alcampo", recipe_limit=5)
    assert isinstance(run, ShadowEvaluationRun)
    assert run.status == "completed"
    assert run.baseline_provider == "demo"
    # Never touches production.
    activation = db_session.execute(
        select(ProviderActivation).where(ProviderActivation.provider_code == "parsebot-alcampo")
    ).scalar_one()
    assert activation.activation_state == "shadow"
    assert activation.production_eligibility is False
    assert activation.production_approved_at is None
    assert before is None  # was not production-approved before either


def test_unknown_cost_is_null_not_zero(db_session: Session) -> None:
    # With no fully-costable recipe, absence of a known cost is NULL — never 0 € / -100%.
    run = run_provider_shadow(db_session, "parsebot-alcampo", recipe_limit=10)
    assert run.comparison_status == "no_costable_recipes"
    assert run.basket_known_cost is None
    assert run.absolute_difference is None
    assert run.percentage_difference is None
    assert "no_costable_recipes" in (run.comparison_blockers or [])


def test_activation_gates_are_not_contradictory(db_session: Session) -> None:
    run_provider_shadow(db_session, "parsebot-alcampo", recipe_limit=5)
    a = db_session.execute(
        select(ProviderActivation).where(ProviderActivation.provider_code == "parsebot-alcampo")
    ).scalar_one()
    # Enabled up to shadow, production strictly OFF — orthogonal gates never contradict.
    assert a.transport_enabled and a.capture_enabled and a.normalization_enabled
    assert a.staging_enabled and a.shadow_enabled
    assert a.production_enabled is False and a.production_approved is False
    assert a.production_eligibility is False


def test_readiness_reports_without_activating(db_session: Session) -> None:
    before = db_session.execute(
        select(ProviderActivation.activation_state).where(
            ProviderActivation.provider_code == "parsebot-alcampo"
        )
    ).scalar_one_or_none()
    report = evaluate_production_readiness(db_session, "parsebot-alcampo")
    assert report.candidate_for_production_partial is False
    assert report.would_change_state is False
    assert "rights_not_approved" in report.blocking_reasons
    after = db_session.execute(
        select(ProviderActivation.activation_state).where(
            ProviderActivation.provider_code == "parsebot-alcampo"
        )
    ).scalar_one_or_none()
    assert after == before  # the report never mutated state


def test_e2e_shadow_coverage_no_cross_contamination(
    db_session: Session, variant: ProductVariant
) -> None:
    # A real production price for a demo-like variant + staging noise on the same variant.
    db_session.add(_obs(variant, amount="1.20", staging=False))
    db_session.add(_obs(variant, amount="8.80", staging=True))
    db_session.flush()

    # Coverage + shadow run over the provider's staging data (honest, low).
    cov = evaluate_recipe_catalog_coverage(
        db_session, "parsebot-carrefour", scope="staging", recipe_limit=8
    )
    run = run_provider_shadow(db_session, "parsebot-carrefour", recipe_limit=8)
    assert cov.fully_costable_recipes == 0
    assert run.recipes_costable == 0

    # Production continuity: the demo-like production price is intact, staging never leaked.
    prod = CurrentPriceService().current(db_session, variant.id, as_of=_NOW, staging=False)
    assert prod is not None and prod.amount == Decimal("1.2000")
