"""Licensed-catalog readiness gate (FASE 5): the eight exit criteria.

A fresh chain fails the gate; a chain that has a validated field map, imported + approved
mappings, a second sync (incremental + history) and an attested licence passes it. Without
the licence attestation the gate still blocks retiring the demo.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from cestaplan_api.ingestion.licensed_catalog import (
    CsvLicensedCatalogImporter,
    SupplierFieldMap,
)
from cestaplan_api.models import IngredientProductMapping, Retailer, SupplierFieldMapping
from cestaplan_api.services.readiness import GateConfig, evaluate_readiness
from cestaplan_api.services.sample_import import run_sample_import

_MAP = SupplierFieldMap(
    field_map={
        "external_id": "sku",
        "product_name": "name",
        "amount": "price",
        "currency": "currency",
        "package_quantity": "qty",
        "package_unit": "unit",
    },
    default_currency="EUR",
)


def _csv(price_leche: str, price_garb: str, price_vin: str) -> str:
    return (
        "sku,name,price,currency,qty,unit\n"
        f"LIC-001,Leche desnatada brick 1 L,{price_leche},EUR,1000,ml\n"
        f"LIC-002,Garbanzos cocidos bote 400 g,{price_garb},EUR,400,g\n"
        f"LIC-003,Vinagre de vino 750 ml,{price_vin},EUR,750,ml\n"
    )


def _retailer(db: Session, slug: str) -> Retailer:
    r = Retailer(slug=slug, name="Gate Chain", adapter_key="feed", is_synthetic=False)
    db.add(r)
    db.flush()
    return r


def _criteria(report) -> dict[str, bool]:
    return {c.key: c.passed for c in report.criteria}


def test_fresh_retailer_fails_gate(db_session: Session) -> None:
    retailer = _retailer(db_session, "gate-fresh")
    report = evaluate_readiness(db_session, retailer)
    assert report.can_retire_demo is False
    c = _criteria(report)
    assert c["ingredient_coverage"] is False
    assert c["incremental_update"] is False
    assert c["price_history"] is False
    assert c["license_verified"] is False


def _full_setup(db_session: Session, retailer: Retailer) -> None:
    # A validated supplier field map exists.
    db_session.add(
        SupplierFieldMapping(
            source_name=f"prov-{retailer.slug}",
            field_map=dict(_MAP.field_map),
            is_active=True,
        )
    )
    db_session.flush()
    # Import + approve every candidate, then a second sync with new prices (history).
    run_sample_import(
        db_session, retailer, _csv("0.88", "0.91", "0.87"), _MAP,
        CsvLicensedCatalogImporter(), dry_run=False,
    )
    for m in db_session.execute(
        select(IngredientProductMapping).where(
            IngredientProductMapping.retailer_id == retailer.id
        )
    ).scalars():
        m.verification_status = "human_verified"
        m.is_active = True
    db_session.flush()
    run_sample_import(
        db_session, retailer, _csv("0.95", "0.99", "0.90"), _MAP,
        CsvLicensedCatalogImporter(), dry_run=False,
    )


def test_full_flow_with_licence_passes_gate(db_session: Session) -> None:
    retailer = _retailer(db_session, "gate-ready")
    _full_setup(db_session, retailer)

    report = evaluate_readiness(
        db_session,
        retailer,
        GateConfig(min_ingredient_coverage=0.02, license_verified=True),
    )
    c = _criteria(report)
    assert c["field_mapping_validated"] is True
    assert c["ingredient_coverage"] is True
    assert c["min_coverage"] is True
    assert c["incremental_update"] is True  # second sync appended a 2nd observation
    assert c["price_history"] is True  # first observation was closed
    assert c["idempotency"] is True  # no duplicate open observations
    assert c["no_critical_errors"] is True
    assert c["license_verified"] is True
    assert report.can_retire_demo is True


def test_missing_licence_blocks_demo_retirement(db_session: Session) -> None:
    retailer = _retailer(db_session, "gate-nolic")
    _full_setup(db_session, retailer)

    report = evaluate_readiness(
        db_session,
        retailer,
        GateConfig(min_ingredient_coverage=0.02, license_verified=False),
    )
    c = _criteria(report)
    # Everything technical passes...
    assert c["ingredient_coverage"] is True
    assert c["incremental_update"] is True
    # ...but the unsigned licence blocks retiring the demo.
    assert c["license_verified"] is False
    assert report.can_retire_demo is False
