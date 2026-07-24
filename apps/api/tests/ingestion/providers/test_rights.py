"""Authorized-source rights registry + idempotent bootstrap (spec §2/§10/§11).

Rights are recorded as *authorized* without ever enabling production, costing, or seeding any
product/price. The bootstrap only FILLS undecided values and never overwrites an admin change.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from cestaplan_api.ingestion.providers.rights import (
    AUTHORIZED_EXTERNAL_CODES,
    SOURCE_RIGHTS,
    get_source_rights,
)
from cestaplan_api.models import Product, ProductPrice, ProviderActivation
from cestaplan_api.tools.bootstrap_source_rights import (
    apply_to_session,
    plan_all,
)

_NOW = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)
_ALL_CODES = list(SOURCE_RIGHTS.keys())


def _clean_activations(db: Session) -> None:
    """Remove any activation rows so we exercise the fresh-DB path (rolled back after the test)."""
    db.execute(delete(ProviderActivation))
    db.flush()


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #


def test_seven_external_sources_are_authorized_commercial() -> None:
    assert len(AUTHORIZED_EXTERNAL_CODES) == 7
    for code in AUTHORIZED_EXTERNAL_CODES:
        r = SOURCE_RIGHTS[code]
        assert r.data_rights_status == "commercial_use_allowed"
        assert r.authorization_status == "verified"
        assert r.authorized_source is True
        assert r.license_basis == "private_commercial_agreement"
        assert r.license_display_name == "Licencia comercial privada"
        assert r.rights_display_name == "Uso autorizado"
        # An intermediary (Parse.bot / Apify) is NEVER an official API.
        assert r.official_api is False
        assert r.technical_provider in ("Parse.bot", "Apify")
        # Raw redistribution stays off; attribution is governed by the private agreement (None).
        assert r.rights_scope["raw_redistribution"] is False
        assert r.rights_scope["attribution_required"] is None
        assert r.rights_scope["commercial_use"] is True


def test_open_prices_keeps_odbl_and_is_official() -> None:
    r = get_source_rights("open-prices")
    assert r is not None
    assert r.data_rights_status == "odbl"
    assert r.license_basis == "odbl"
    assert r.official_api is True
    assert r.technical_provider is None
    assert r.rights_scope["attribution_required"] is True
    assert r.attribution_text_public is not None


def test_demo_is_own_synthetic() -> None:
    r = get_source_rights("demo")
    assert r is not None
    assert r.data_rights_status == "own_synthetic"
    assert r.license_basis == "own_synthetic"
    assert r.official_api is False
    assert r.rights_scope["attribution_required"] is False


# --------------------------------------------------------------------------- #
# Bootstrap
# --------------------------------------------------------------------------- #


def test_bootstrap_on_clean_db_records_rights_without_products_or_prices(
    db_session: Session,
) -> None:
    _clean_activations(db_session)
    products_before = db_session.scalar(select(func.count()).select_from(Product))
    prices_before = db_session.scalar(select(func.count()).select_from(ProductPrice))

    plans = apply_to_session(db_session, _ALL_CODES, _NOW)

    assert all(p.created for p in plans)  # fresh rows for every source
    dia = db_session.execute(
        select(ProviderActivation).where(ProviderActivation.provider_code == "parsebot-dia")
    ).scalar_one()
    assert dia.data_rights_status == "commercial_use_allowed"
    assert dia.authorization_status == "verified"
    assert dia.license_display_name == "Licencia comercial privada"
    assert dia.rights_scope is not None and dia.rights_scope["commercial_use"] is True
    assert dia.authorization_verified_at == _NOW
    # No catalogue data was seeded.
    assert db_session.scalar(select(func.count()).select_from(Product)) == products_before
    assert db_session.scalar(select(func.count()).select_from(ProductPrice)) == prices_before


def test_bootstrap_never_enables_production_or_costing(db_session: Session) -> None:
    _clean_activations(db_session)
    apply_to_session(db_session, _ALL_CODES, _NOW)
    for row in db_session.execute(select(ProviderActivation)).scalars():
        assert row.production_enabled is False
        assert row.production_approved is False
        assert row.production_eligibility is False
        assert row.costing_eligibility == "unknown"
        assert row.mapper_status == "unknown"
        assert row.data_quality_status == "unknown"


def test_bootstrap_is_idempotent(db_session: Session) -> None:
    _clean_activations(db_session)
    apply_to_session(db_session, _ALL_CODES, _NOW)
    # A second pass must plan zero changes.
    second = plan_all(db_session, _ALL_CODES, _NOW)
    still_changing = [p.provider_code for p in second if p.has_changes]
    assert not still_changing, still_changing


def test_bootstrap_does_not_overwrite_admin_decisions(db_session: Session) -> None:
    _clean_activations(db_session)
    # An admin deliberately rejected a source and set an internal note.
    row = ProviderActivation(
        provider_code="parsebot-dia",
        data_rights_status="rejected",
        authorization_status="rejected",
        license_display_name="Anulada por el administrador",
    )
    db_session.add(row)
    db_session.flush()

    plans = plan_all(db_session, ["parsebot-dia"], _NOW)
    changed_fields = {c.field for c in plans[0].changes}
    # None of the operator-decided fields are touched.
    assert "data_rights_status" not in changed_fields
    assert "authorization_status" not in changed_fields
    assert "license_display_name" not in changed_fields


def test_bootstrap_never_touches_internal_evidence_fields(db_session: Session) -> None:
    _clean_activations(db_session)
    plans = apply_to_session(db_session, _ALL_CODES, _NOW)
    for plan in plans:
        fields = {c.field for c in plan.changes}
        assert "internal_evidence_reference" not in fields
        assert "legal_notes_internal" not in fields
    row = db_session.execute(
        select(ProviderActivation).where(ProviderActivation.provider_code == "parsebot-dia")
    ).scalar_one()
    assert row.internal_evidence_reference is None
    assert row.legal_notes_internal is None
