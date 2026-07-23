"""Provider-agnostic licensed-catalog import (FASE 2).

Covers field resolution (dotted paths, unit aliases, comma decimals, net-content default,
required-field errors), the CSV/JSON importers, and persistence (variant net content,
dry-run writes nothing, idempotency, and append-only history on a newer observation).
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from cestaplan_api.ingestion.licensed_catalog import (
    CsvLicensedCatalogImporter,
    JsonLicensedCatalogImporter,
    SupplierFieldMap,
    persist_records,
    resolve_record,
)
from cestaplan_api.models import PriceObservation, ProductVariant, Retailer

# A supplier map with the supplier's own field names (note: no canonical_name).
_MAP = SupplierFieldMap(
    field_map={
        "external_id": "sku",
        "product_name": "title",
        "brand": "marca",
        "amount": "precio.valor",
        "currency": "precio.moneda",
        "sell_unit": "venta",
        "package_quantity": "envase.cantidad",
        "package_unit": "envase.unidad",
        "store_external_id": "tienda",
    },
    unit_aliases={"gramos": "g"},
    default_currency="EUR",
)


def _retailer(db: Session, slug: str) -> Retailer:
    r = Retailer(slug=slug, name="Licensed", adapter_key="feed", is_synthetic=False)
    db.add(r)
    db.flush()
    return r


def _garbanzos_payload() -> dict:
    return {
        "sku": "SUP-GARB-400",
        "title": "Garbanzos cocidos 400 g",
        "marca": "MarcaX",
        "precio": {"valor": "0,91", "moneda": "eur"},
        "venta": "unit",
        "envase": {"cantidad": "400", "unidad": "gramos"},
    }


# --- field resolution ------------------------------------------------------ #
def test_resolve_dotted_paths_units_and_comma_decimal() -> None:
    record, errors = resolve_record(_garbanzos_payload(), _MAP)
    assert errors == []
    assert record is not None
    assert record.external_id == "SUP-GARB-400"
    assert record.amount == Decimal("0.91")  # comma decimal parsed, no float
    assert record.currency == "EUR"
    assert record.package_unit == "g"  # alias 'gramos' -> 'g'
    assert record.sell_unit == "unit"
    # Net content defaults to the package when it is a mass/volume unit -> costable.
    assert record.net_content_quantity == Decimal("400")
    assert record.net_content_unit == "g"


def test_resolve_missing_required_fields_reported() -> None:
    record, errors = resolve_record({"title": "x"}, _MAP)
    assert record is None
    assert any("external_id" in e for e in errors)
    assert any("amount" in e for e in errors)


def test_resolve_rejects_invalid_sell_unit() -> None:
    payload = _garbanzos_payload()
    payload["venta"] = "caja"
    record, errors = resolve_record(payload, _MAP)
    assert record is None
    assert any("sell_unit" in e for e in errors)


def test_resolve_non_positive_amount_rejected() -> None:
    payload = _garbanzos_payload()
    payload["precio"]["valor"] = "0"
    record, errors = resolve_record(payload, _MAP)
    assert record is None
    assert any("non-positive" in e for e in errors)


# --- importers ------------------------------------------------------------- #
def test_csv_importer_parses_and_reports_bad_rows() -> None:
    csv_map = SupplierFieldMap(
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
    raw = (
        "sku,name,price,currency,qty,unit\n"
        "A1,Leche 1 L,0.88,EUR,1000,ml\n"
        ",Broken sin sku,1.00,EUR,1,unit\n"  # missing external_id -> row error
    )
    records, errors = CsvLicensedCatalogImporter().to_records(raw, csv_map)
    assert [r.external_id for r in records] == ["A1"]
    assert records[0].net_content_unit == "ml"
    assert len(errors) == 1 and errors[0].row_index == 1


def test_json_importer_items_path() -> None:
    item = (
        '{"sku":"J1","title":"Vinagre 750 ml",'
        '"precio":{"valor":"0.87","moneda":"EUR"},'
        '"envase":{"cantidad":"750","unidad":"ml"}}'
    )
    raw = '{"data": {"items": [' + item + "]}}"
    importer = JsonLicensedCatalogImporter(items_path="data.items")
    records, errors = importer.to_records(raw, _MAP)
    assert errors == []
    assert records[0].external_id == "J1"
    assert records[0].net_content_unit == "ml"


# --- persistence ----------------------------------------------------------- #
def _obs_count(db: Session, variant_id: int) -> int:
    return db.scalar(
        select(func.count(PriceObservation.id)).where(
            PriceObservation.product_variant_id == variant_id
        )
    ) or 0


def test_persist_creates_variant_with_net_content_and_observation(db_session: Session) -> None:
    retailer = _retailer(db_session, "lic-persist")
    record, _ = resolve_record(_garbanzos_payload(), _MAP)
    assert record is not None

    outcome = persist_records(db_session, retailer, [record])
    assert outcome.variants_created == 1
    assert outcome.observations_inserted == 1
    assert outcome.costable_variants == 1

    variant = db_session.execute(
        select(ProductVariant).where(ProductVariant.retailer_id == retailer.id)
    ).scalars().one()
    assert variant.net_content_quantity == Decimal("400.0000")
    assert variant.net_content_unit == "g"
    assert _obs_count(db_session, variant.id) == 1


def test_persist_dry_run_writes_nothing(db_session: Session) -> None:
    retailer = _retailer(db_session, "lic-dry")
    record, _ = resolve_record(_garbanzos_payload(), _MAP)
    assert record is not None

    outcome = persist_records(db_session, retailer, [record], dry_run=True)
    # Counts reflect what WOULD happen...
    assert outcome.dry_run is True
    assert outcome.variants_created == 1
    assert outcome.observations_inserted == 1
    # ...but nothing was written.
    assert db_session.scalar(
        select(func.count(ProductVariant.id)).where(ProductVariant.retailer_id == retailer.id)
    ) == 0


def test_persist_is_idempotent(db_session: Session) -> None:
    retailer = _retailer(db_session, "lic-idem")
    record, _ = resolve_record(_garbanzos_payload(), _MAP)
    assert record is not None

    first = persist_records(db_session, retailer, [record])
    second = persist_records(db_session, retailer, [record])

    assert first.observations_inserted == 1
    assert second.observations_inserted == 0
    assert second.observations_skipped == 1
    assert second.variants_created == 0

    variant = db_session.execute(
        select(ProductVariant).where(ProductVariant.retailer_id == retailer.id)
    ).scalars().one()
    assert _obs_count(db_session, variant.id) == 1  # no duplicate history


def test_persist_incremental_appends_history(db_session: Session) -> None:
    from datetime import UTC, datetime, timedelta

    retailer = _retailer(db_session, "lic-hist")
    record, _ = resolve_record(_garbanzos_payload(), _MAP)
    assert record is not None

    t0 = datetime(2026, 7, 20, 9, 0, tzinfo=UTC)
    record.observed_at = t0
    persist_records(db_session, retailer, [record], as_of=t0)

    # A newer observation with a different price appends a row and closes the prior one.
    record.observed_at = t0 + timedelta(days=1)
    record.amount = Decimal("0.95")
    outcome = persist_records(db_session, retailer, [record], as_of=t0 + timedelta(days=1))
    assert outcome.observations_inserted == 1

    variant = db_session.execute(
        select(ProductVariant).where(ProductVariant.retailer_id == retailer.id)
    ).scalars().one()
    obs = db_session.execute(
        select(PriceObservation)
        .where(PriceObservation.product_variant_id == variant.id)
        .order_by(PriceObservation.valid_from)
    ).scalars().all()
    assert len(obs) == 2
    assert obs[0].valid_until == t0 + timedelta(days=1)  # prior row closed
    assert obs[1].valid_until is None  # newest row open
