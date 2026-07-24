"""Candidate enrichment (spec §6/§7) — service-level, fetcher injected, NO network."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from cestaplan_api.config import Settings
from cestaplan_api.models import Ingredient, ProviderIngredientMapping, ProviderUsage, User
from cestaplan_api.services import mapping_enrichment as enr
from cestaplan_api.services.mapping_enrichment import EnrichmentFailed

_NOW = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)


def _settings(**over: Any) -> Settings:
    base: dict[str, Any] = {"enrichment_daily_budget": 50, "enrichment_min_seconds_between": 5}
    base.update(over)
    return Settings(**base)


def _user(db: Session) -> int:
    u = User(email=f"e-{id(db)}@x.com", password_hash="x", display_name="E")
    db.add(u)
    db.flush()
    return u.id


def _cand(
    db: Session, *, provider: str, key: str, name: str, ext: str
) -> ProviderIngredientMapping:
    ing = db.execute(select(Ingredient).where(Ingredient.canonical_name == key)).scalar_one()
    row = ProviderIngredientMapping(
        provider_code=provider,
        ingredient_id=ing.id,
        canonical_ingredient_key=key,
        retailer_slug=provider.split("-")[-1],
        external_product_id=ext,
        mapping_status="candidate",
        mapping_method="normalized_name",
        confidence_score=Decimal("0.6"),
        required_review=True,
        active=False,
        relation_status="competing",
        evidence_json={"product_name": name, "warnings": []},
    )
    db.add(row)
    db.flush()
    return row


def _fetcher(detail: dict[str, Any]):
    def _f(provider_code: str, external_product_id: str, settings: Settings) -> dict[str, Any]:
        return detail

    return _f


def test_completed_keeps_previous_and_stores_only_sanitized(db_session: Session) -> None:
    row = _cand(
        db_session, provider="parsebot-alcampo", key="tomate", name="Tomate pera", ext="ENR-1"
    )
    detail = {
        "category": "verduras",
        "unit": "kg",
        "price": "1.20",
        "x-api-key": "SECRET",
        "cookie": "c=1",
        "raw_body": {"h": 1},
    }
    enr.enrich(
        db_session,
        row.id,
        requested_by=_user(db_session),
        settings=_settings(),
        now=_NOW,
        detail_fetcher=_fetcher(detail),
    )
    assert row.enrichment_status == "completed"
    ev = row.evidence_json or {}
    assert ev["enriched"]["category"] == "verduras"
    # Secrets/raw are NEVER stored.
    assert "x-api-key" not in ev["enriched"] and "cookie" not in ev["enriched"]
    assert "raw_body" not in ev["enriched"]
    assert ev["previous_evidence"]["product_name"] == "Tomate pera"  # preserved


def test_failed_records_error_category(db_session: Session) -> None:
    row = _cand(db_session, provider="parsebot-alcampo", key="sal", name="Sal", ext="ENR-2")

    def _boom(*_a: Any, **_k: Any) -> dict[str, Any]:
        raise EnrichmentFailed("transport", "Timeout")

    enr.enrich(
        db_session,
        row.id,
        requested_by=_user(db_session),
        settings=_settings(),
        now=_NOW,
        detail_fetcher=_boom,
    )
    assert row.enrichment_status == "failed" and row.enrichment_error_category == "transport"


def test_unavailable_provider_without_detail_endpoint(db_session: Session) -> None:
    row = _cand(db_session, provider="parsebot-lidl", key="sal", name="Sal", ext="ENR-3")
    enr.enrich(
        db_session,
        row.id,
        requested_by=_user(db_session),
        settings=_settings(),
        now=_NOW,
        detail_fetcher=_fetcher({"category": "x"}),
    )
    assert row.enrichment_status == "unavailable"


def test_budget_exceeded(db_session: Session) -> None:
    row = _cand(db_session, provider="parsebot-alcampo", key="ajo", name="Ajo", ext="ENR-4")
    enr.enrich(
        db_session,
        row.id,
        requested_by=_user(db_session),
        settings=_settings(enrichment_daily_budget=0),
        now=_NOW,
        detail_fetcher=_fetcher({"category": "verduras"}),
    )
    assert row.enrichment_status == "budget_exceeded"


def test_rate_limited(db_session: Session) -> None:
    row = _cand(db_session, provider="parsebot-alcampo", key="cebolla", name="Cebolla", ext="ENR-5")
    row.enrichment_requested_at = _NOW - timedelta(seconds=1)  # very recent
    db_session.flush()
    enr.enrich(
        db_session,
        row.id,
        requested_by=_user(db_session),
        settings=_settings(enrichment_min_seconds_between=60),
        now=_NOW,
        detail_fetcher=_fetcher({"category": "verduras"}),
    )
    assert row.enrichment_status == "rate_limited"


def test_alcampo_deterministic_autoapproves(db_session: Session) -> None:
    row = _cand(
        db_session,
        provider="parsebot-alcampo",
        key="aceite_oliva",
        name="Aceite de oliva virgen extra 1 L",
        ext="ENR-6",
    )
    enr.enrich(
        db_session,
        row.id,
        requested_by=_user(db_session),
        settings=_settings(),
        now=_NOW,
        detail_fetcher=_fetcher({"category": "aceites_condimentos", "unit": "l"}),
    )
    assert row.mapping_status == "auto_approved" and row.active is True


def test_carrefour_never_autoapproves_under_critical_explosion(db_session: Session) -> None:
    row = _cand(
        db_session,
        provider="parsebot-carrefour",
        key="aceite_oliva",
        name="Aceite de oliva virgen extra 1 L",
        ext="ENR-7",
    )
    enr.enrich(
        db_session,
        row.id,
        requested_by=_user(db_session),
        settings=_settings(),
        now=_NOW,
        detail_fetcher=_fetcher({"category": "aceites_condimentos", "unit": "l"}),
    )
    # deterministic rule would auto-approve, but Carrefour's explosion is critical -> stays review.
    assert row.mapping_status != "auto_approved" and row.active is False
    assert row.enrichment_status == "completed"


def test_usage_is_logged(db_session: Session) -> None:
    row = _cand(db_session, provider="parsebot-alcampo", key="patata", name="Patata", ext="ENR-8")
    enr.enrich(
        db_session,
        row.id,
        requested_by=_user(db_session),
        settings=_settings(),
        now=_NOW,
        detail_fetcher=_fetcher({"category": "verduras"}),
    )
    used = (
        db_session.execute(
            select(ProviderUsage).where(
                ProviderUsage.operation == "enrichment",
                ProviderUsage.provider == "parsebot-alcampo",
            )
        )
        .scalars()
        .all()
    )
    assert len(used) >= 1
