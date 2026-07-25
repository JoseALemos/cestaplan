"""Product-first discovery: matching reads staged products and writes ONLY candidate rows — it
never creates a PriceObservation/ExternalProduct per candidate. Candidates are bounded, review-only,
and idempotent. This is the fix for the Carrefour candidate/observation explosion."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from cestaplan_api.models import (
    ExternalProduct,
    PriceObservation,
    ProductPrice,
    ProviderIngredientMapping,
)
from cestaplan_api.services.targeted_discovery import (
    _MAX_CANDIDATES_PER_PRODUCT,
    ApprovalMode,
    discover_and_map,
)
from tests.fixtures.provider_scenarios import (
    ensure_test_ingredient,
    seed_test_catalog_product,
    seed_test_retailer,
)

_KEYS = ["leche_entera", "aceite_oliva", "tomate", "patata", "cebolla"]


def _staging_obs(db: Session, retailer_id: int) -> int:
    return int(
        db.scalar(
            select(func.count()).select_from(PriceObservation).where(
                PriceObservation.retailer_id == retailer_id,
                PriceObservation.staging_only.is_(True),
            )
        )
        or 0
    )


def _externals(db: Session, retailer_id: int) -> int:
    return int(
        db.scalar(
            select(func.count()).select_from(ExternalProduct).where(
                ExternalProduct.retailer_id == retailer_id
            )
        )
        or 0
    )


def _cf_candidates(db: Session):
    # Scoped to THIS test's products so ambient parsebot-carrefour rows never pollute the counts.
    return db.execute(
        select(ProviderIngredientMapping).where(
            ProviderIngredientMapping.provider_code == "parsebot-carrefour",
            ProviderIngredientMapping.external_product_id.in_(["CF-1", "CF-2"]),
        )
    ).scalars().all()


def _seed_carrefour(db: Session):
    for key in _KEYS:
        ensure_test_ingredient(db, key)
    retailer = seed_test_retailer(db, "carrefour")
    # Two staged products, each with exactly ONE staging observation.
    seed_test_catalog_product(db, retailer, "CF-1", name="Leche entera 1L", price="1.19")
    seed_test_catalog_product(
        db, retailer, "CF-2", name="Aceite de oliva virgen extra 1L", price="6.5"
    )
    return retailer


def test_carrefour_matching_creates_no_new_observations_or_products(db_session: Session) -> None:
    retailer = _seed_carrefour(db_session)
    obs_before = _staging_obs(db_session, retailer.id)
    ext_before = _externals(db_session, retailer.id)
    price_before = int(db_session.scalar(select(func.count()).select_from(ProductPrice)) or 0)

    discover_and_map(
        db_session, "parsebot-carrefour", _KEYS, approval_mode=ApprovalMode.REVIEW_ONLY
    )

    # Matching wrote NO new staging observation and NO new external product (the explosion fix).
    assert _staging_obs(db_session, retailer.id) == obs_before
    assert _externals(db_session, retailer.id) == ext_before
    # And never a productive price.
    price_after = int(db_session.scalar(select(func.count()).select_from(ProductPrice)) or 0)
    assert price_after == price_before


def test_candidates_are_bounded_and_review_only(db_session: Session) -> None:
    _seed_carrefour(db_session)
    discover_and_map(
        db_session, "parsebot-carrefour", _KEYS, approval_mode=ApprovalMode.REVIEW_ONLY
    )
    cands = _cf_candidates(db_session)
    assert cands  # at least the obvious leche/aceite matches
    # No product exceeds the per-product bound.
    from collections import Counter

    per_product = Counter(c.external_product_id for c in cands)
    assert max(per_product.values()) <= _MAX_CANDIDATES_PER_PRODUCT
    # Every candidate is review-only, v2, never active/reviewed.
    for c in cands:
        assert c.active is False
        assert c.reviewed_at is None and c.reviewed_by is None
        assert c.required_review is True
        assert c.mapping_status == "candidate"
        assert c.mapping_version == "2.0.0"


def test_repeat_discovery_is_idempotent(db_session: Session) -> None:
    retailer = _seed_carrefour(db_session)
    discover_and_map(
        db_session, "parsebot-carrefour", _KEYS, approval_mode=ApprovalMode.REVIEW_ONLY
    )
    obs_after_first = _staging_obs(db_session, retailer.id)
    cands_after_first = len(_cf_candidates(db_session))

    discover_and_map(
        db_session, "parsebot-carrefour", _KEYS, approval_mode=ApprovalMode.REVIEW_ONLY
    )
    # A second identical run adds no observations and no duplicate candidates (all stay review-only,
    # active=false — proven in test_candidates_are_bounded_and_review_only).
    assert _staging_obs(db_session, retailer.id) == obs_after_first
    assert len(_cf_candidates(db_session)) == cands_after_first
