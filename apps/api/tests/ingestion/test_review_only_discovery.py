"""Review-only candidate discovery: never activates a mapping, preserves the machine proposal, and
is the enforced cloud default. Deterministic auto-approval is an explicit, cloud-blocked opt-in."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from cestaplan_api.config import Settings
from cestaplan_api.models import IngredientProductMapping, ProviderIngredientMapping
from cestaplan_api.services.mapping_review import is_selectable_for_costing
from cestaplan_api.services.targeted_discovery import ApprovalMode, _upsert_mapping
from tests.fixtures.provider_scenarios import (
    ensure_test_ingredient,
    seed_test_catalog_product,
    seed_test_retailer,
)

NOW = datetime(2026, 7, 25, 15, 0, tzinfo=UTC)


def _cand(status: str = "auto_approved") -> SimpleNamespace:
    return SimpleNamespace(
        mapping_status=status,
        mapping_method="exact_alias",
        confidence=Decimal("0.9500"),
        lexical_score=Decimal("0.9"),
        category_score=Decimal("0.9"),
        semantic_score=Decimal("0.9"),
        unit_compatibility="compatible",
        preparation_compatibility="compatible",
        dietary_compatibility="compatible",
        allergen_compatibility="compatible",
        required_review=False,
        as_dict=lambda: {"mapping_status": status},
    )


def _setup(db: Session) -> tuple[int, int]:
    ing = ensure_test_ingredient(db, "leche_entera")
    retailer = seed_test_retailer(db, "carrefour")
    product, _variant = seed_test_catalog_product(db, retailer, "X1", name="Leche entera 1L")
    return ing.id, product.id


def _row(db: Session, ingredient_id: int) -> ProviderIngredientMapping:
    # Scope to this test's exact (provider, ingredient, external product) so ambient rows for the
    # real "leche_entera" ingredient never collide.
    return db.execute(
        select(ProviderIngredientMapping).where(
            ProviderIngredientMapping.provider_code == "parsebot-carrefour",
            ProviderIngredientMapping.ingredient_id == ingredient_id,
            ProviderIngredientMapping.external_product_id == "X1",
        )
    ).scalar_one()


def test_review_only_never_activates_and_preserves_proposal(db_session: Session) -> None:
    ing_id, product_id = _setup(db_session)
    prod = SimpleNamespace(external_product_id="X1", product_name="Leche entera 1L")
    active_before = db_session.scalar(
        select(func.count()).select_from(IngredientProductMapping).where(
            IngredientProductMapping.is_active.is_(True)
        )
    )
    # Even with active=True passed in, review-only forces a non-active candidate.
    _upsert_mapping(
        db_session, "parsebot-carrefour", "carrefour", ing_id, "leche_entera", prod, product_id,
        _cand("auto_approved"), active=True, now=NOW, approval_mode=ApprovalMode.REVIEW_ONLY,
    )
    row = _row(db_session, ing_id)
    assert row.active is False
    assert row.reviewed_at is None
    assert row.reviewed_by is None
    assert row.required_review is True
    assert row.mapping_status == "candidate"
    # The machine's proposal is preserved separately.
    assert row.proposed_mapping_status == "auto_approved"
    assert row.proposed_confidence == Decimal("0.9500")
    assert row.proposed_method == "exact_alias"
    # Not selectable for costing, and no productive mapping was created.
    assert is_selectable_for_costing(row) is False
    assert db_session.scalar(
        select(func.count()).select_from(IngredientProductMapping).where(
            IngredientProductMapping.is_active.is_(True)
        )
    ) == active_before


def test_deterministic_activates_and_records_proposal(db_session: Session) -> None:
    ing_id, product_id = _setup(db_session)
    prod = SimpleNamespace(external_product_id="X1", product_name="Leche entera 1L")
    _upsert_mapping(
        db_session, "parsebot-carrefour", "carrefour", ing_id, "leche_entera", prod, product_id,
        _cand("auto_approved"), active=True, now=NOW,
        approval_mode=ApprovalMode.DETERMINISTIC_AUTOAPPROVAL,
    )
    row = _row(db_session, ing_id)
    assert row.active is True
    assert row.mapping_status == "auto_approved"
    assert row.reviewed_at is not None
    assert row.proposed_mapping_status == "auto_approved"  # proposal still recorded
    assert is_selectable_for_costing(row) is True


def test_cloud_blocks_deterministic_autoapproval(monkeypatch: pytest.MonkeyPatch) -> None:
    from cestaplan_api.jobs import discover_provider_candidates as cli

    monkeypatch.setattr(
        cli, "get_settings",
        lambda: Settings(deployment_mode="cloud", allow_deterministic_autoapproval=False),
    )
    rc = cli.run("parsebot-dia", ["leche_entera"], ApprovalMode.DETERMINISTIC_AUTOAPPROVAL, 10)
    assert rc == 2  # blocked before touching the DB


def test_cloud_allows_deterministic_with_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    from cestaplan_api.jobs import discover_provider_candidates as cli

    monkeypatch.setattr(
        cli, "get_settings",
        lambda: Settings(deployment_mode="cloud", allow_deterministic_autoapproval=True),
    )
    # Not blocked at the gate (would proceed to discovery); we don't run the network here, just
    # assert the gate does not short-circuit to rc==2.
    monkeypatch.setattr(
        cli, "discover_and_map", lambda *a, **k: SimpleNamespace(as_dict=lambda: {})
    )

    class _NoDB:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def commit(self):
            pass

    monkeypatch.setattr(cli, "SessionLocal", lambda: _NoDB())
    rc = cli.run("parsebot-dia", ["leche_entera"], ApprovalMode.DETERMINISTIC_AUTOAPPROVAL, 10)
    assert rc == 0


def test_cli_default_mode_is_review_only(monkeypatch: pytest.MonkeyPatch) -> None:
    from cestaplan_api.jobs import discover_provider_candidates as cli

    captured: dict[str, object] = {}
    monkeypatch.setattr(cli, "run", lambda p, k, mode, limit: captured.update(mode=mode) or 0)
    monkeypatch.setattr("sys.argv", ["prog", "--provider", "parsebot-dia"])
    with pytest.raises(SystemExit):
        cli.main()
    assert captured["mode"] is ApprovalMode.REVIEW_ONLY
