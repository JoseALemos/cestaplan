"""Alcampo detail-endpoint contract: fingerprint + adapter + enrichment (audit §5) — offline."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from cestaplan_api.config import Settings
from cestaplan_api.models import Ingredient, ProviderIngredientMapping, User
from cestaplan_api.services import mapping_enrichment as enr
from cestaplan_api.services.mapping_enrichment import (
    _DETAIL_CONTRACT_FIELDS,
    _DETAIL_CONTRACT_FINGERPRINT,
    _adapt_alcampo_detail,
    detail_contract_fingerprint,
)

_NOW = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)

# Synthetic fixture mirroring the OBSERVED Alcampo detail structure — fabricated values, no secrets.
_ALCAMPO_DETAIL_FIXTURE: dict[str, Any] = {
    "name": "Plátano de Canarias bandeja 700 g",
    "brand": "MARCA DEMO",
    "packSizeDescription": "700g",
    "type": "REGULAR",
    "price": {"amount": "2.49", "currency": "EUR"},
    "unitPrice": {
        "price": {"amount": "3.56", "currency": "EUR"},
        "unit": "fop.price.per.kg",
        "unitName": "PER_1KG",
    },
    "categoryPath": ["Frescos", "Frutas", "Plátanos y Bananas"],
    "ingredients": None,
    "retailerProductId": "616252",
    "productId": "synthetic-uuid",
}


def _settings() -> Settings:
    return Settings(enrichment_daily_budget=50, enrichment_min_seconds_between=5)


def _user(db: Session) -> int:
    u = User(email=f"dc-{id(db)}@x.com", password_hash="x", display_name="DC")
    db.add(u)
    db.flush()
    return u.id


def _cand(db: Session, ext: str) -> ProviderIngredientMapping:
    ing = db.execute(select(Ingredient).where(Ingredient.canonical_name == "platano")).scalar_one()
    row = ProviderIngredientMapping(
        provider_code="parsebot-alcampo",
        ingredient_id=ing.id,
        canonical_ingredient_key="platano",
        retailer_slug="alcampo",
        external_product_id=ext,
        mapping_status="candidate",
        mapping_method="normalized_name",
        confidence_score=Decimal("0.6"),
        required_review=True,
        active=False,
        evidence_json={"product_name": "Plátano de Canarias bandeja 700 g", "warnings": []},
    )
    db.add(row)
    db.flush()
    return row


def test_observed_structure_matches_pinned_fingerprint() -> None:
    fp = detail_contract_fingerprint(
        _ALCAMPO_DETAIL_FIXTURE, _DETAIL_CONTRACT_FIELDS["parsebot-alcampo"]
    )
    assert fp == _DETAIL_CONTRACT_FINGERPRINT["parsebot-alcampo"]


def test_unknown_structure_has_a_different_fingerprint() -> None:
    mutated = dict(_ALCAMPO_DETAIL_FIXTURE)
    mutated["unitPrice"] = "just-a-string"  # a genuinely different shape
    fp = detail_contract_fingerprint(mutated, _DETAIL_CONTRACT_FIELDS["parsebot-alcampo"])
    assert fp != _DETAIL_CONTRACT_FINGERPRINT["parsebot-alcampo"]


def test_adapter_maps_to_sanitized_vocabulary() -> None:
    out = _adapt_alcampo_detail(_ALCAMPO_DETAIL_FIXTURE)
    assert out["category"] == "frutas"
    assert out["net_content"] == "700g"
    assert out["unit"] == "g"
    assert out["price"] == "2.49"
    assert out["unit_price"] == "3.56"
    assert out["unit_price_unit"] == "kg"
    # Never leaks images / ids / raw structures.
    assert "image" not in out and "productId" not in out


def test_enrichment_completes_with_the_observed_structure(db_session: Session) -> None:
    row = _cand(db_session, "ENR-DETAIL-1")

    def _fetcher(
        provider_code: str, external_product_id: str, settings: Settings
    ) -> dict[str, Any]:
        # The default fetcher would fingerprint-gate + adapt; here we inject the adapted result.
        return _adapt_alcampo_detail(_ALCAMPO_DETAIL_FIXTURE)

    enr.enrich(
        db_session,
        row.id,
        requested_by=_user(db_session),
        settings=_settings(),
        now=_NOW,
        detail_fetcher=_fetcher,
    )
    assert row.enrichment_status == "completed"
    assert (row.evidence_json or {})["enriched"]["category"] == "frutas"


def test_unknown_fingerprint_blocks_only_enrichment_not_search(db_session: Session) -> None:
    row = _cand(db_session, "ENR-DETAIL-2")

    def _boom(provider_code: str, external_product_id: str, settings: Settings) -> dict[str, Any]:
        raise enr.EnrichmentFailed("detail_contract_unknown")

    enr.enrich(
        db_session,
        row.id,
        requested_by=_user(db_session),
        settings=_settings(),
        now=_NOW,
        detail_fetcher=_boom,
    )
    # Enrichment failed with the precise category, but the mapping is untouched and still usable
    # by the search/classification pipeline (the failure never blocks the provider).
    assert row.enrichment_status == "failed"
    assert row.enrichment_error_category == "detail_contract_unknown"
    assert row.mapping_status == "candidate"  # search-side state preserved
    still = db_session.get(ProviderIngredientMapping, row.id)
    assert still is not None and still.external_product_id == "ENR-DETAIL-2"
