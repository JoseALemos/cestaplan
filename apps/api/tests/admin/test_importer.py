"""Import service tests: validation, dry-run, idempotent commit, duplicates, rollback."""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from cestaplan_api.models import (
    Ingredient,
    IngredientProductMapping,
    Product,
    ProductBarcode,
    ProductPrice,
    Retailer,
)
from cestaplan_api.services import importer

_HEADER = (
    "retailer_slug,store_external_code,store_postal_code,product_external_id,product_name,"
    "brand,category,barcode,package_quantity,package_unit,amount,currency,unit_price,"
    "promotion,availability,source_type,source_name,source_url,observed_at,expires_at,"
    "confidence_score,verification_status"
)


def _row(
    *,
    retailer="acme",
    store="ACME-1",
    product="ACME-CHK-500",
    name="Pollo 500 g",
    barcode="8400000000017",
    qty="500",
    unit="g",
    amount="3.49",
    unit_price="6.98",
    source_type="admin_import",
    observed_at="2026-07-20T08:00:00Z",
) -> str:
    return (
        f"{retailer},{store},28013,{product},{name},MarcaX,carnes,{barcode},{qty},{unit},"
        f"{amount},EUR,{unit_price},,in_stock,{source_type},Cat operador,,"
        f"{observed_at},2026-08-20T08:00:00Z,0.9,unverified"
    )


def _csv(*rows: str) -> str:
    return "\n".join([_HEADER, *rows]) + "\n"


def _price_count(db: Session) -> int:
    return db.execute(select(func.count(ProductPrice.id))).scalar_one()


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #
def test_valid_row_parses_to_decimal_money() -> None:
    row: dict[str, str] = dict(zip(_HEADER.split(","), _row().split(","), strict=True))
    result = importer.build_record(row, 1)
    assert not result.errors
    assert result.record is not None
    assert result.record.amount == Decimal("3.49")
    assert isinstance(result.record.amount, Decimal)
    assert result.record.package_quantity == Decimal("500")


def test_missing_amount_is_rejected_not_zeroed(db_session: Session) -> None:
    di = importer.create_import(
        db_session, content=_csv(_row(amount="")), fmt="csv", dry_run=True
    )
    assert di.error_count == 1
    assert di.summary is not None
    errors = di.summary["errors"]
    assert any(e["field"] == "amount" for e in errors)
    # No price invented for the bad row.
    assert di.created_count == 0


def test_non_positive_amount_rejected(db_session: Session) -> None:
    di = importer.create_import(
        db_session, content=_csv(_row(amount="0")), fmt="csv", dry_run=True
    )
    assert di.error_count == 1
    assert di.summary is not None
    assert any(
        e["field"] == "amount" and ">" in e["message"] for e in di.summary["errors"]
    )


def test_unit_price_mismatch_flagged(db_session: Session) -> None:
    # 3.49 for 500 g -> ~6.98 €/kg; declaring 99.0 €/kg is inconsistent.
    di = importer.create_import(
        db_session, content=_csv(_row(unit_price="99.0")), fmt="csv", dry_run=True
    )
    assert di.error_count == 1
    assert di.summary is not None
    assert any(e["field"] == "unit_price" for e in di.summary["errors"])


def test_consistent_unit_price_accepted(db_session: Session) -> None:
    di = importer.create_import(
        db_session, content=_csv(_row(unit_price="6.98")), fmt="csv", dry_run=True
    )
    assert di.error_count == 0
    assert di.created_count == 1


# --------------------------------------------------------------------------- #
# Dry-run
# --------------------------------------------------------------------------- #
def test_dry_run_writes_no_prices_but_records_a_data_import(db_session: Session) -> None:
    before = _price_count(db_session)
    di = importer.create_import(
        db_session, content=_csv(_row()), fmt="csv", dry_run=True, filename="s.csv"
    )
    assert di.status == "dry_run"
    assert di.row_count == 1
    assert di.created_count == 1
    assert _price_count(db_session) == before  # nothing written
    # No product created either during a dry run.
    assert (
        db_session.execute(
            select(func.count(Product.id)).where(Product.external_id == "ACME-CHK-500")
        ).scalar_one()
        == 0
    )


# --------------------------------------------------------------------------- #
# Commit + idempotency
# --------------------------------------------------------------------------- #
def test_commit_writes_prices_and_tags_import_id(db_session: Session) -> None:
    di = importer.create_import(db_session, content=_csv(_row()), fmt="csv", dry_run=False)
    assert di.status == "pending"
    importer.commit_import(db_session, di)
    db_session.flush()
    assert di.status == "committed"
    prices = db_session.execute(
        select(ProductPrice).where(ProductPrice.import_id == di.id)
    ).scalars().all()
    assert len(prices) == 1
    assert prices[0].amount == Decimal("3.49")
    # Barcode captured for the imported product (scoped, not a global count, so the
    # assertion holds regardless of other real/demo data committed in the dev DB).
    assert (
        db_session.execute(
            select(func.count(ProductBarcode.id)).where(
                ProductBarcode.product_id == prices[0].product_id
            )
        ).scalar_one()
        == 1
    )


def test_commit_is_idempotent_no_duplicate_explosion(db_session: Session) -> None:
    di1 = importer.create_import(db_session, content=_csv(_row()), fmt="csv", dry_run=False)
    importer.commit_import(db_session, di1)
    db_session.flush()
    after_first = _price_count(db_session)

    # Same observation again (new import batch) -> skipped, no new price row.
    di2 = importer.create_import(db_session, content=_csv(_row()), fmt="csv", dry_run=False)
    importer.commit_import(db_session, di2)
    db_session.flush()
    assert _price_count(db_session) == after_first
    assert di2.created_count == 0
    assert di2.skipped_count == 1


def test_new_observation_appends_history(db_session: Session) -> None:
    di1 = importer.create_import(db_session, content=_csv(_row()), fmt="csv", dry_run=False)
    importer.commit_import(db_session, di1)
    db_session.flush()
    before = _price_count(db_session)

    # Same product/store, later observation -> appended (history preserved).
    di2 = importer.create_import(
        db_session,
        content=_csv(_row(observed_at="2026-07-27T08:00:00Z")),
        fmt="csv",
        dry_run=False,
    )
    importer.commit_import(db_session, di2)
    db_session.flush()
    assert _price_count(db_session) == before + 1
    assert di2.updated_count == 1


def test_duplicate_within_batch_detected(db_session: Session) -> None:
    di = importer.create_import(
        db_session, content=_csv(_row(), _row()), fmt="csv", dry_run=False
    )
    # Two identical observation rows: one accepted, one flagged duplicate.
    assert di.row_count == 2
    assert di.created_count == 1
    assert di.skipped_count == 1
    assert di.summary is not None
    assert any("duplicada" in e["message"] for e in di.summary["errors"])
    importer.commit_import(db_session, di)
    db_session.flush()
    assert (
        db_session.execute(
            select(func.count(ProductPrice.id)).where(ProductPrice.import_id == di.id)
        ).scalar_one()
        == 1
    )


# --------------------------------------------------------------------------- #
# Rollback
# --------------------------------------------------------------------------- #
def test_rollback_removes_only_batch_prices_leaves_products(db_session: Session) -> None:
    di = importer.create_import(db_session, content=_csv(_row()), fmt="csv", dry_run=False)
    importer.commit_import(db_session, di)
    db_session.flush()
    product_id = db_session.execute(
        select(Product.id).where(Product.external_id == "ACME-CHK-500")
    ).scalar_one()

    deleted = importer.rollback_import(db_session, di)
    db_session.flush()
    assert deleted == 1
    assert di.status == "rolled_back"
    assert di.rolled_back_at is not None
    # Prices gone...
    assert (
        db_session.execute(
            select(func.count(ProductPrice.id)).where(ProductPrice.import_id == di.id)
        ).scalar_one()
        == 0
    )
    # ...product still present.
    assert db_session.get(Product, product_id) is not None
    # Retailer created by the import also remains.
    assert (
        db_session.execute(
            select(func.count(Retailer.id)).where(Retailer.slug == "acme")
        ).scalar_one()
        == 1
    )


def test_cannot_commit_twice(db_session: Session) -> None:
    di = importer.create_import(db_session, content=_csv(_row()), fmt="csv", dry_run=False)
    importer.commit_import(db_session, di)
    db_session.flush()
    try:
        importer.commit_import(db_session, di)
    except ValueError as exc:
        assert "committed" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("committing an already-committed import should raise")


# --------------------------------------------------------------------------- #
# Explicit canonical_name -> IngredientProductMapping
# --------------------------------------------------------------------------- #
_CANON_HEADER = _HEADER + ",canonical_name"


def _canon_row(*, canonical_name: str, observed_at: str = "2026-07-20T08:00:00Z") -> str:
    return _row(observed_at=observed_at) + f",{canonical_name}"


def _canon_csv(*rows: str) -> str:
    return "\n".join([_CANON_HEADER, *rows]) + "\n"


def _make_ingredient(db: Session, canonical_name: str) -> Ingredient:
    ingredient = Ingredient(
        canonical_name=canonical_name,
        display_name=canonical_name.replace("_", " ").title(),
        default_unit="g",
    )
    db.add(ingredient)
    db.flush()
    return ingredient


def _mapping_count(db: Session, ingredient_id: int) -> int:
    return db.execute(
        select(func.count(IngredientProductMapping.id)).where(
            IngredientProductMapping.ingredient_id == ingredient_id
        )
    ).scalar_one()


def test_canonical_name_creates_ingredient_mapping(db_session: Session) -> None:
    ingredient = _make_ingredient(db_session, "test_canon_pollo")
    # Uppercase in the CSV: matching is case-insensitive.
    di = importer.create_import(
        db_session,
        content=_canon_csv(_canon_row(canonical_name="TEST_CANON_POLLO")),
        fmt="csv",
        dry_run=False,
    )
    assert di.error_count == 0
    assert di.summary is not None
    assert not di.summary["warnings"]
    importer.commit_import(db_session, di)
    db_session.flush()

    mapping = db_session.execute(
        select(IngredientProductMapping).where(
            IngredientProductMapping.ingredient_id == ingredient.id
        )
    ).scalar_one()
    assert mapping.confidence_score == Decimal("1.0000")
    assert mapping.retailer_id is not None
    assert mapping.is_active is True
    # The mapped product is the imported one.
    product = db_session.get(Product, mapping.product_id)
    assert product is not None and product.external_id == "ACME-CHK-500"
    assert di.summary["commit"]["mapped"] == 1


def test_unmatched_canonical_name_warns_but_imports_product(db_session: Session) -> None:
    di = importer.create_import(
        db_session,
        content=_canon_csv(_canon_row(canonical_name="no_such_ingredient_zzz")),
        fmt="csv",
        dry_run=False,
    )
    # Not a hard failure: the row is valid and the product still imports.
    assert di.error_count == 0
    assert di.created_count == 1
    assert di.summary is not None
    warnings = di.summary["warnings"]
    assert len(warnings) == 1
    assert warnings[0]["field"] == "canonical_name"
    assert "no_such_ingredient_zzz" in warnings[0]["message"]

    importer.commit_import(db_session, di)
    db_session.flush()
    # Product imported, but no mapping was created for the unmatched name.
    assert (
        db_session.execute(
            select(func.count(Product.id)).where(Product.external_id == "ACME-CHK-500")
        ).scalar_one()
        == 1
    )
    assert di.summary is not None
    assert di.summary["commit"]["mapped"] == 0


def test_canonical_name_mapping_is_idempotent(db_session: Session) -> None:
    ingredient = _make_ingredient(db_session, "test_canon_arroz")
    di1 = importer.create_import(
        db_session,
        content=_canon_csv(_canon_row(canonical_name="test_canon_arroz")),
        fmt="csv",
        dry_run=False,
    )
    importer.commit_import(db_session, di1)
    db_session.flush()
    assert _mapping_count(db_session, ingredient.id) == 1

    # A later observation of the same product with the same canonical_name -> no duplicate.
    di2 = importer.create_import(
        db_session,
        content=_canon_csv(
            _canon_row(
                canonical_name="test_canon_arroz",
                observed_at="2026-07-27T08:00:00Z",
            )
        ),
        fmt="csv",
        dry_run=False,
    )
    importer.commit_import(db_session, di2)
    db_session.flush()
    assert _mapping_count(db_session, ingredient.id) == 1
    assert di2.summary is not None
    assert di2.summary["commit"]["mapped"] == 0


def test_json_import_same_fields(db_session: Session) -> None:
    payload = (
        '[{"retailer_slug":"acme","store_external_code":"ACME-1","product_external_id":'
        '"ACME-RICE","product_name":"Arroz 1kg","package_quantity":"1000","package_unit":'
        '"g","amount":"1.19","currency":"EUR","source_type":"admin_import","source_name":'
        '"Cat","observed_at":"2026-07-20T08:00:00Z"}]'
    )
    di = importer.create_import(db_session, content=payload, fmt="json", dry_run=False)
    assert di.error_count == 0
    assert di.created_count == 1
    importer.commit_import(db_session, di)
    db_session.flush()
    assert (
        db_session.execute(
            select(func.count(ProductPrice.id)).where(ProductPrice.import_id == di.id)
        ).scalar_one()
        == 1
    )
