"""Staging → production promotion bridge (phase 2).

The staging-first invariant is the point of these tests: a provider's captured prices and mapping
candidates NEVER reach the productive tables (``ProductPrice`` / active
``IngredientProductMapping``) until (a) a human has approved the provider for production and (b) an
explicit promotion runs. A
blocked gate writes nothing; an approved promotion materializes exactly the approved data and is
idempotent.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from cestaplan_api.config import Settings
from cestaplan_api.models import (
    IngredientProductMapping,
    PriceObservation,
    ProductPrice,
    User,
)
from cestaplan_api.services.provider_promotion import (
    PromotionBlocked,
    approve_provider_production,
    promote_provider_to_production,
    promotion_status,
)
from tests.fixtures.provider_scenarios import (
    ensure_test_ingredient,
    seed_test_catalog_product,
    seed_test_mapping_candidate,
    seed_test_provider_activation,
    seed_test_retailer,
    seed_test_store,
)

PROVIDER = "test_provider"
NOW = datetime(2026, 7, 25, 12, 0, tzinfo=UTC)
# Providers are disabled by default; a promotion needs them enabled AND the rights approved.
ENABLED = Settings(price_providers_enabled=True)


def _actor(db: Session, email: str = "promoter@test.local") -> int:
    """Create a platform admin and return its id (production_approved_by is a user FK)."""
    user = User(email=email, password_hash="x", is_admin=True)
    db.add(user)
    db.flush()
    return user.id


def _staging_count(db: Session, retailer_id: int) -> int:
    return int(
        db.scalar(
            select(func.count()).select_from(PriceObservation).where(
                PriceObservation.retailer_id == retailer_id,
                PriceObservation.staging_only.is_(True),
            )
        )
        or 0
    )


def _ready_activation(db: Session, **overrides: object):
    """Activation whose non-approval prerequisites are all met (still NOT production-approved)."""
    activation = seed_test_provider_activation(db, PROVIDER)
    activation.transport_status = "operational"
    activation.mapper_status = "verified"
    activation.data_quality_status = "accepted"
    activation.data_rights_status = "commercial_use_allowed"
    for key, value in overrides.items():
        setattr(activation, key, value)
    db.flush()
    return activation


def _promotable_candidate(db: Session):
    """A fully approved candidate resolving to a real product with a STAGING price at a store."""
    retailer = seed_test_retailer(db, PROVIDER)
    store = seed_test_store(db, retailer)
    ingredient = ensure_test_ingredient(db, "aceite_de_oliva")
    product, variant = seed_test_catalog_product(
        db, retailer, "OP-1", name="Aceite de oliva 1L", net_qty="1", net_unit="l"
    )
    # A staging observation WITH a store (project_current_prices needs a store to project).
    db.add(
        PriceObservation(
            retailer_id=retailer.id,
            store_id=store.id,
            product_variant_id=variant.id,
            price_scope="national",
            price_type="regular",
            amount=Decimal("4.19"),
            currency="EUR",
            observed_at=NOW,
            imported_at=NOW,
            valid_from=NOW,
            confidence_score=Decimal("1.0"),
            staging_only=True,
        )
    )
    db.flush()
    candidate = seed_test_mapping_candidate(
        db,
        PROVIDER,
        ingredient,
        "OP-1",
        retailer_slug=PROVIDER,
        mapping_status="manually_approved",
        active=True,
        normalized_product_id=product.id,
    )
    return retailer, product, candidate


def _prod_counts(db: Session, retailer_id: int) -> tuple[int, int]:
    prices = int(
        db.scalar(
            select(func.count()).select_from(ProductPrice).where(
                ProductPrice.retailer_id == retailer_id
            )
        )
        or 0
    )
    active_maps = int(
        db.scalar(
            select(func.count()).select_from(IngredientProductMapping).where(
                IngredientProductMapping.is_active.is_(True),
                IngredientProductMapping.retailer_id == retailer_id,
            )
        )
        or 0
    )
    return prices, active_maps


# --------------------------------------------------------------------------- #
# Production approval
# --------------------------------------------------------------------------- #
def test_approve_refuses_until_prerequisites_hold(db_session: Session) -> None:
    _ready_activation(db_session, mapper_status="unknown")  # one prerequisite missing
    with pytest.raises(PromotionBlocked) as exc:
        approve_provider_production(
            db_session, provider_code=PROVIDER, actor_id=1, settings=ENABLED
        )
    assert any("mapper_status" in r for r in exc.value.reasons)


def test_approve_sets_flags_actor_and_is_idempotent(db_session: Session) -> None:
    _ready_activation(db_session)
    actor = _actor(db_session)
    activation = approve_provider_production(
        db_session, provider_code=PROVIDER, actor_id=actor, settings=ENABLED
    )
    assert activation.production_enabled is True
    assert activation.production_approved is True
    assert activation.production_approved_by == actor
    assert activation.production_approved_at is not None
    first_at = activation.production_approved_at
    # A second approval keeps the ORIGINAL approver + timestamp (audit is not overwritten).
    again = approve_provider_production(
        db_session, provider_code=PROVIDER, actor_id=actor + 1, settings=ENABLED
    )
    assert again.production_approved_by == actor
    assert again.production_approved_at == first_at


def test_approve_blocked_when_providers_disabled(db_session: Session) -> None:
    _ready_activation(db_session)
    with pytest.raises(PromotionBlocked) as exc:
        approve_provider_production(
            db_session, provider_code=PROVIDER, actor_id=1, settings=Settings()
        )
    assert "price_providers_disabled" in exc.value.reasons


# --------------------------------------------------------------------------- #
# Promotion gate
# --------------------------------------------------------------------------- #
def test_promote_refuses_and_writes_nothing_when_not_approved(db_session: Session) -> None:
    _ready_activation(db_session)  # prerequisites met but NOT production-approved
    retailer, _product, _candidate = _promotable_candidate(db_session)
    before = _prod_counts(db_session, retailer.id)
    with pytest.raises(PromotionBlocked):
        promote_provider_to_production(
            db_session, provider_code=PROVIDER, actor_id=1, settings=ENABLED
        )
    # No productive rows and the observation stays staging-only.
    assert _prod_counts(db_session, retailer.id) == before == (0, 0)
    assert _staging_count(db_session, retailer.id) == 1


# --------------------------------------------------------------------------- #
# Promotion happy path + idempotency
# --------------------------------------------------------------------------- #
def test_promote_materializes_mappings_and_projects_prices(db_session: Session) -> None:
    _ready_activation(db_session)
    retailer, product, _candidate = _promotable_candidate(db_session)
    assert _prod_counts(db_session, retailer.id) == (0, 0)  # nothing productive before

    actor = _actor(db_session)
    approve_provider_production(
        db_session, provider_code=PROVIDER, actor_id=actor, settings=ENABLED
    )
    result = promote_provider_to_production(
        db_session, provider_code=PROVIDER, actor_id=actor, settings=ENABLED
    )

    assert result.approved_candidates == 1
    assert result.mappings_created == 1
    assert result.observations_promoted == 1
    assert result.prices_written == 1

    # A productive, active mapping now links the ingredient to the canonical product.
    mapping = db_session.execute(
        select(IngredientProductMapping).where(
            IngredientProductMapping.product_id == product.id
        )
    ).scalar_one()
    assert mapping.is_active is True
    assert mapping.verification_status == "human_verified"
    assert mapping.verified_by == actor

    # The staged observation is now production, and a ProductPrice exists for the chain.
    prices, active_maps = _prod_counts(db_session, retailer.id)
    assert prices == 1
    assert active_maps == 1
    assert _staging_count(db_session, retailer.id) == 0


def test_promote_is_idempotent(db_session: Session) -> None:
    _ready_activation(db_session)
    retailer, _product, _candidate = _promotable_candidate(db_session)
    actor = _actor(db_session)
    approve_provider_production(
        db_session, provider_code=PROVIDER, actor_id=actor, settings=ENABLED
    )

    first = promote_provider_to_production(
        db_session, provider_code=PROVIDER, actor_id=actor, settings=ENABLED
    )
    assert first.mappings_created == 1
    prices_after_first, maps_after_first = _prod_counts(db_session, retailer.id)

    second = promote_provider_to_production(
        db_session, provider_code=PROVIDER, actor_id=actor, settings=ENABLED
    )
    assert second.mappings_created == 0  # mapping already exists
    # No duplicated productive rows on a second run.
    assert _prod_counts(db_session, retailer.id) == (prices_after_first, maps_after_first)


# --------------------------------------------------------------------------- #
# Status
# --------------------------------------------------------------------------- #
def test_promotion_status_reports_gate_and_counts(db_session: Session) -> None:
    _ready_activation(db_session)
    _promotable_candidate(db_session)
    status = promotion_status(db_session, provider_code=PROVIDER, settings=ENABLED)
    assert status["production_ready"] is False  # not approved yet
    assert "not_manually_approved" in status["gate_reasons"]  # type: ignore[operator]
    assert status["approved_candidates"] == 1
    assert status["staged_observations"] == 1
