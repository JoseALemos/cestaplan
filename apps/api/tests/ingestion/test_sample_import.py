"""Sample-import pipeline (FASE 3): the 10-step licensed-sample validation flow.

Covers schema validation, dry-run (nothing written), the error/coverage reports, duplicate
detection, unit/price/geographic validation, mapping-candidate generation with ingredient
coverage, and the review queue (inactive machine candidates on commit).
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from cestaplan_api.ingestion.licensed_catalog import (
    CsvLicensedCatalogImporter,
    SupplierFieldMap,
)
from cestaplan_api.models import IngredientProductMapping, ProductVariant, Retailer
from cestaplan_api.services.sample_import import run_sample_import

_MAP = SupplierFieldMap(
    field_map={
        "external_id": "sku",
        "product_name": "name",
        "amount": "price",
        "currency": "currency",
        "package_quantity": "qty",
        "package_unit": "unit",
        "store_external_id": "store",
    },
    default_currency="EUR",
)

# 7 rows: 3 match seeded ingredients, one counted (not costable), one duplicate sku,
# one implausible price, one referencing a store that does not exist.
_CSV = (
    "sku,name,price,currency,qty,unit,store\n"
    "LIC-001,Leche desnatada brick 1 L,0.88,EUR,1000,ml,\n"
    "LIC-002,Garbanzos cocidos bote 400 g,0.91,EUR,400,g,\n"
    "LIC-003,Vinagre de vino 750 ml,0.87,EUR,750,ml,\n"
    "LIC-004,Producto raro xyz,2.50,EUR,1,unit,\n"
    "LIC-002,Garbanzos duplicado,0.95,EUR,400,g,\n"
    "LIC-005,Caro implausible,2000,EUR,1000,ml,\n"
    "LIC-006,Con tienda fantasma,1.00,EUR,500,g,STORE-XYZ\n"
)


def _retailer(db: Session, slug: str) -> Retailer:
    r = Retailer(slug=slug, name="Licensed sample", adapter_key="feed", is_synthetic=False)
    db.add(r)
    db.flush()
    return r


def test_schema_validation_blocks_missing_required_field() -> None:
    bad_map = SupplierFieldMap(field_map={"external_id": "sku", "product_name": "name"})
    # No DB work needed: schema validation short-circuits before parsing.
    report = run_sample_import(
        db=None,  # type: ignore[arg-type]
        retailer=None,  # type: ignore[arg-type]
        raw=_CSV,
        field_map=bad_map,
        importer=CsvLicensedCatalogImporter(),
    )
    assert report.ok is False
    assert report.schema_errors and "amount" in report.schema_errors[0]


def test_dry_run_full_report_writes_nothing(db_session: Session) -> None:
    retailer = _retailer(db_session, "sample-dry")
    report = run_sample_import(
        db_session, retailer, _CSV, _MAP, CsvLicensedCatalogImporter()
    )

    assert report.dry_run is True
    assert report.ok is True
    # duplicate detection
    assert report.duplicates == ["LIC-002"]
    assert report.distinct_products == 6
    # coverage: 5 of the 6 distinct products carry a costable mass/volume net content
    assert report.costable_products == 5
    # price + geo validation
    assert any(i.code == "amount_implausible" for i in report.price_warnings)
    assert any(i.code == "store_unresolved" for i in report.geo_warnings)
    # dry-run persist counts computed...
    assert report.persist.variants_created == 6
    assert report.persist.observations_inserted == 6
    # ...but nothing written
    assert (
        db_session.scalar(
            select(func.count(ProductVariant.id)).where(
                ProductVariant.retailer_id == retailer.id
            )
        )
        == 0
    )
    # mapping candidates: at least the 3 clearly-named products match seeded ingredients
    assert report.products_matched >= 2
    assert report.ingredients_covered >= 2
    assert report.ingredients_total == 75
    assert len(report.review_queue) == report.products_matched


def test_commit_persists_products_and_inactive_candidates(db_session: Session) -> None:
    retailer = _retailer(db_session, "sample-commit")
    report = run_sample_import(
        db_session, retailer, _CSV, _MAP, CsvLicensedCatalogImporter(), dry_run=False
    )
    assert report.dry_run is False

    # products/variants persisted
    variants = (
        db_session.execute(
            select(ProductVariant).where(ProductVariant.retailer_id == retailer.id)
        )
        .scalars()
        .all()
    )
    assert len(variants) == 6
    assert any(v.net_content_unit == "g" for v in variants)

    # candidates written as INACTIVE, machine-verified mappings (the review queue)
    mappings = (
        db_session.execute(
            select(IngredientProductMapping).where(
                IngredientProductMapping.retailer_id == retailer.id
            )
        )
        .scalars()
        .all()
    )
    assert mappings, "expected at least one machine candidate"
    for m in mappings:
        assert m.is_active is False
        assert m.verification_status == "machine_verified"
        assert m.product_variant_id is not None
        assert m.match_method == "token"


def test_commit_is_idempotent_on_candidates(db_session: Session) -> None:
    retailer = _retailer(db_session, "sample-idem")
    run_sample_import(
        db_session, retailer, _CSV, _MAP, CsvLicensedCatalogImporter(), dry_run=False
    )
    first = db_session.scalar(
        select(func.count(IngredientProductMapping.id)).where(
            IngredientProductMapping.retailer_id == retailer.id
        )
    )
    run_sample_import(
        db_session, retailer, _CSV, _MAP, CsvLicensedCatalogImporter(), dry_run=False
    )
    second = db_session.scalar(
        select(func.count(IngredientProductMapping.id)).where(
            IngredientProductMapping.retailer_id == retailer.id
        )
    )
    assert first is not None and first == second and first >= 1
