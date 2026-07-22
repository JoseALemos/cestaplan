"""Import service: the section-20 CSV/JSON pipeline (parse → validate → dry-run → commit).

Design guarantees, straight from the canonical rules:

- **Never invents a price.** A missing/``<=0`` ``amount`` or a missing ``observed_at`` fails
  validation; absence is never turned into ``0``.
- **Append-only history.** A price refresh is a *new* ``ProductPrice`` observation, never a
  destructive UPDATE of the previous one.
- **Idempotent.** Re-importing the same observation ``(store, product, observed_at)`` is a
  no-op (skipped), so committing the same data twice does not explode the price history.
- **Reversible.** Every committed price row is tagged with its ``DataImport.id``; a logical
  rollback removes exactly those rows (prices only — products are left in place).

The two-phase flow persists the validated records inside ``DataImport.summary`` so a later
``commit`` re-materialises them without needing the file re-uploaded.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from cestaplan_api.adapters.base import NormalizedRecord, RawRow
from cestaplan_api.adapters.files import CsvRetailerAdapter, JsonRetailerAdapter
from cestaplan_api.models import (
    DataImport,
    DataSource,
    Product,
    ProductBarcode,
    ProductPrice,
    Retailer,
    Store,
)
from cestaplan_api.models.catalog import AVAILABILITY, SOURCE_TYPE, VERIFICATION_STATUS
from cestaplan_api.services.audit import record_audit

_REQUIRED_FIELDS = (
    "retailer_slug",
    "store_external_code",
    "product_external_id",
    "product_name",
    "package_quantity",
    "package_unit",
    "amount",
    "currency",
    "source_type",
    "source_name",
    "observed_at",
)

# Factor to convert a package quantity into its price-per base unit (€/kg, €/l, €/unit).
_UNIT_BASE: dict[str, Decimal] = {
    "mg": Decimal("0.000001"),
    "g": Decimal("0.001"),
    "kg": Decimal("1"),
    "ml": Decimal("0.001"),
    "cl": Decimal("0.01"),
    "l": Decimal("1"),
    "unit": Decimal("1"),
    "ud": Decimal("1"),
    "u": Decimal("1"),
}

# Default confidence by source_type (ADAPTER_GUIDE.md §4.1 midpoints).
_DEFAULT_CONFIDENCE: dict[str, Decimal] = {
    "official": Decimal("0.95"),
    "authorized_partner": Decimal("0.95"),
    "admin_import": Decimal("0.85"),
    "manual_entry": Decimal("0.65"),
    "user_receipt": Decimal("0.65"),
    "community_connector": Decimal("0.50"),
    "open_dataset": Decimal("0.50"),
    "estimated": Decimal("0.20"),
    "demo": Decimal("1.00"),
}

_CONFIDENCE_Q = Decimal("0.0001")
_UNIT_PRICE_TOLERANCE_REL = Decimal("0.02")
_UNIT_PRICE_TOLERANCE_ABS = Decimal("0.02")
_SAMPLE_LIMIT = 25


# --------------------------------------------------------------------------- #
# Value objects
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class RowError:
    """A structured per-row validation error."""

    row: int
    field: str | None
    message: str


@dataclass(slots=True)
class RowValidation:
    """Outcome of validating one raw row into a normalized record."""

    record: NormalizedRecord | None = None
    errors: list[RowError] = field(default_factory=list)


@dataclass(slots=True)
class PlanEntry:
    """What one record would do on commit (``created`` / ``updated`` / ``skipped``)."""

    row: int
    action: str
    product_external_id: str
    store_external_code: str
    amount: str


@dataclass(slots=True)
class PlanResult:
    """Aggregate of planned/applied changes for a batch of records."""

    created: int = 0
    updated: int = 0
    skipped: int = 0
    entries: list[PlanEntry] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Parsing helpers
# --------------------------------------------------------------------------- #
def _decimal(value: str) -> Decimal | None:
    try:
        return Decimal(value)
    except (InvalidOperation, ValueError):
        return None


def _parse_dt(value: str) -> datetime | None:
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def default_confidence(source_type: str) -> Decimal:
    """Confidence to assume when a row omits ``confidence_score``."""
    return _DEFAULT_CONFIDENCE.get(source_type, Decimal("0.50"))


def build_record(row: RawRow, index: int = 0) -> RowValidation:
    """Validate one canonical raw row into a :class:`NormalizedRecord` or errors.

    Enforces required fields, Decimal money (``amount > 0``, never ``0`` for absence),
    enumerations, ISO-8601 ``observed_at`` and — when both are present — the consistency of
    ``unit_price`` against ``amount / package_quantity`` (so €/kg is never mistaken for the
    package price).
    """
    errors: list[RowError] = []

    def err(field_name: str | None, message: str) -> None:
        errors.append(RowError(row=index, field=field_name, message=message))

    for field_name in _REQUIRED_FIELDS:
        if not row.get(field_name):
            err(field_name, "campo obligatorio ausente")

    amount = _decimal(row["amount"]) if row.get("amount") else None
    if row.get("amount") and amount is None:
        err("amount", "importe no numérico")
    elif amount is not None and amount <= 0:
        err("amount", "el importe del envase debe ser > 0 (una ausencia nunca es 0)")

    package_quantity = (
        _decimal(row["package_quantity"]) if row.get("package_quantity") else None
    )
    if row.get("package_quantity") and package_quantity is None:
        err("package_quantity", "cantidad de envase no numérica")
    elif package_quantity is not None and package_quantity <= 0:
        err("package_quantity", "la cantidad de envase debe ser > 0")

    unit_price = _decimal(row["unit_price"]) if row.get("unit_price") else None
    if row.get("unit_price") and unit_price is None:
        err("unit_price", "precio unitario no numérico")

    confidence = _decimal(row["confidence_score"]) if row.get("confidence_score") else None
    if row.get("confidence_score") and confidence is None:
        err("confidence_score", "confianza no numérica")
    elif confidence is not None and not (Decimal("0") <= confidence <= Decimal("1")):
        err("confidence_score", "confianza fuera de rango [0, 1]")

    source_type = row.get("source_type", "")
    if source_type and source_type not in SOURCE_TYPE:
        err("source_type", f"source_type inválido: {source_type}")

    verification = row.get("verification_status") or "unverified"
    if verification not in VERIFICATION_STATUS:
        err("verification_status", f"verification_status inválido: {verification}")

    availability = row.get("availability") or None
    if availability and availability not in AVAILABILITY:
        err("availability", f"availability inválida: {availability}")

    observed_at = _parse_dt(row["observed_at"]) if row.get("observed_at") else None
    if row.get("observed_at") and observed_at is None:
        err("observed_at", "observed_at no es ISO-8601 válida")

    expires_at = None
    if row.get("expires_at"):
        expires_at = _parse_dt(row["expires_at"])
        if expires_at is None:
            err("expires_at", "expires_at no es ISO-8601 válida")

    # unit_price consistency (only if we have both a valid amount and quantity + unit_price).
    if unit_price is not None and amount is not None and package_quantity:
        factor = _UNIT_BASE.get(row.get("package_unit", ""))
        if factor is not None:
            base_quantity = package_quantity * factor
            if base_quantity > 0:
                expected = amount / base_quantity
                tolerance = max(
                    expected * _UNIT_PRICE_TOLERANCE_REL, _UNIT_PRICE_TOLERANCE_ABS
                )
                if abs(expected - unit_price) > tolerance:
                    err(
                        "unit_price",
                        (
                            f"precio unitario incoherente: esperado ~{expected:.4f} "
                            f"(amount/{row.get('package_unit')} base), recibido {unit_price}"
                        ),
                    )

    if errors:
        return RowValidation(errors=errors)

    assert amount is not None and package_quantity is not None and observed_at is not None
    record = NormalizedRecord(
        retailer_slug=row["retailer_slug"],
        store_external_code=row["store_external_code"],
        product_external_id=row["product_external_id"],
        product_name=row["product_name"],
        package_quantity=package_quantity,
        package_unit=row["package_unit"],
        amount=amount,
        currency=row["currency"],
        source_type=source_type,
        source_name=row["source_name"],
        observed_at=observed_at,
        store_province=row.get("store_province") or None,
        store_locality=row.get("store_locality") or None,
        store_postal_code=row.get("store_postal_code") or None,
        brand=row.get("brand") or None,
        category=row.get("category") or None,
        barcode=row.get("barcode") or None,
        unit_price=unit_price,
        promotion=row.get("promotion") or None,
        availability=availability,
        source_url=row.get("source_url") or None,
        expires_at=expires_at,
        confidence_score=confidence,
        verification_status=verification,
    )
    return RowValidation(record=record)


# --------------------------------------------------------------------------- #
# Planning (dry-run) — read-only simulation of what a commit would change
# --------------------------------------------------------------------------- #
def _obs_key(record: NormalizedRecord) -> tuple[str, str, str, str]:
    return (
        record.retailer_slug,
        record.store_external_code,
        record.product_external_id,
        record.observed_at.isoformat(),
    )


def _product_exists_in_db(db: Session, record: NormalizedRecord) -> bool:
    return (
        db.execute(
            select(Product.id)
            .join(Retailer, Retailer.id == Product.retailer_id)
            .where(
                Retailer.slug == record.retailer_slug,
                Product.external_id == record.product_external_id,
            )
        ).first()
        is not None
    )


def _observation_exists_in_db(db: Session, record: NormalizedRecord) -> bool:
    return (
        db.execute(
            select(ProductPrice.id)
            .join(Product, Product.id == ProductPrice.product_id)
            .join(Retailer, Retailer.id == Product.retailer_id)
            .join(Store, Store.id == ProductPrice.store_id)
            .where(
                Retailer.slug == record.retailer_slug,
                Product.external_id == record.product_external_id,
                Store.external_code == record.store_external_code,
                ProductPrice.observed_at == record.observed_at,
            )
        ).first()
        is not None
    )


def plan_changes(db: Session, records: list[NormalizedRecord]) -> PlanResult:
    """Simulate a commit: classify each record as created/updated/skipped. Writes nothing.

    Batch-cumulative: a product created earlier in the same batch counts existing rows
    later in it as ``updated``, so the dry-run counts match the eventual commit exactly.
    """
    result = PlanResult()
    created_products: set[tuple[str, str]] = set()
    for i, record in enumerate(records, start=1):
        prod_key = (record.retailer_slug, record.product_external_id)
        prod_exists = prod_key in created_products or _product_exists_in_db(db, record)
        if not prod_exists:
            action = "created"
            created_products.add(prod_key)
            result.created += 1
        elif _observation_exists_in_db(db, record):
            action = "skipped"
            result.skipped += 1
        else:
            action = "updated"
            result.updated += 1
        if len(result.entries) < _SAMPLE_LIMIT:
            result.entries.append(
                PlanEntry(
                    row=i,
                    action=action,
                    product_external_id=record.product_external_id,
                    store_external_code=record.store_external_code,
                    amount=str(record.amount),
                )
            )
    return result


# --------------------------------------------------------------------------- #
# Persistence (commit)
# --------------------------------------------------------------------------- #
def _get_or_create_retailer(db: Session, record: NormalizedRecord) -> Retailer:
    retailer = db.execute(
        select(Retailer).where(Retailer.slug == record.retailer_slug)
    ).scalar_one_or_none()
    if retailer is None:
        retailer = Retailer(
            slug=record.retailer_slug,
            name=record.retailer_slug.replace("-", " ").title(),
            adapter_key=record.retailer_slug,
            country="ES",
            is_active=True,
            is_synthetic=False,
        )
        db.add(retailer)
        db.flush()
    return retailer


def _get_or_create_store(
    db: Session, retailer: Retailer, record: NormalizedRecord
) -> Store:
    store = db.execute(
        select(Store).where(
            Store.retailer_id == retailer.id,
            Store.external_code == record.store_external_code,
        )
    ).scalar_one_or_none()
    if store is None:
        store = Store(
            retailer_id=retailer.id,
            external_code=record.store_external_code,
            name=record.store_external_code,
            province=record.store_province,
            locality=record.store_locality,
            postal_code=record.store_postal_code,
            is_active=True,
            is_synthetic=False,
        )
        db.add(store)
        db.flush()
    return store


def _get_or_create_product(
    db: Session, retailer: Retailer, record: NormalizedRecord
) -> tuple[Product, bool]:
    product = db.execute(
        select(Product).where(
            Product.retailer_id == retailer.id,
            Product.external_id == record.product_external_id,
        )
    ).scalar_one_or_none()
    if product is None:
        product = Product(
            retailer_id=retailer.id,
            external_id=record.product_external_id,
            name=record.product_name,
            brand=record.brand,
            package_quantity=record.package_quantity,
            package_unit=record.package_unit,
            category_code=record.category,
            is_synthetic=False,
        )
        db.add(product)
        db.flush()
        return product, True
    # Existing product: refresh catalogue fields non-destructively.
    product.name = record.product_name
    if record.brand is not None:
        product.brand = record.brand
    if record.category is not None:
        product.category_code = record.category
    product.package_quantity = record.package_quantity
    product.package_unit = record.package_unit
    return product, False


def _ensure_barcode(db: Session, product: Product, record: NormalizedRecord) -> None:
    if not record.barcode:
        return
    exists = db.execute(
        select(ProductBarcode.id).where(
            ProductBarcode.product_id == product.id,
            ProductBarcode.barcode == record.barcode,
        )
    ).first()
    if exists:
        return
    has_any = db.execute(
        select(ProductBarcode.id).where(ProductBarcode.product_id == product.id)
    ).first()
    db.add(
        ProductBarcode(
            product_id=product.id,
            barcode=record.barcode,
            is_primary=has_any is None,
        )
    )


def commit_records(
    db: Session, records: list[NormalizedRecord], *, import_id: int, now: datetime
) -> PlanResult:
    """Upsert entities and append price observations, tagging each with ``import_id``.

    Idempotent: an observation whose ``(store, product, observed_at)`` already exists is
    skipped, so re-committing the same data never duplicates price history.
    """
    result = PlanResult()
    for i, record in enumerate(records, start=1):
        retailer = _get_or_create_retailer(db, record)
        store = _get_or_create_store(db, retailer, record)
        product, created = _get_or_create_product(db, retailer, record)
        _ensure_barcode(db, product, record)

        duplicate = db.execute(
            select(ProductPrice.id).where(
                ProductPrice.store_id == store.id,
                ProductPrice.product_id == product.id,
                ProductPrice.observed_at == record.observed_at,
            )
        ).first()

        if created:
            action = "created"
            result.created += 1
        elif duplicate is not None:
            action = "skipped"
            result.skipped += 1
        else:
            action = "updated"
            result.updated += 1

        if duplicate is None:
            confidence = (
                record.confidence_score
                if record.confidence_score is not None
                else default_confidence(record.source_type)
            ).quantize(_CONFIDENCE_Q)
            db.add(
                ProductPrice(
                    retailer_id=retailer.id,
                    store_id=store.id,
                    product_id=product.id,
                    amount=record.amount,
                    currency=record.currency,
                    package_quantity=record.package_quantity,
                    package_unit=record.package_unit,
                    unit_price=record.unit_price,
                    promotion=record.promotion,
                    availability=record.availability,
                    source_type=record.source_type,
                    source_name=record.source_name,
                    source_url=record.source_url,
                    observed_at=record.observed_at,
                    imported_at=now,
                    expires_at=record.expires_at,
                    confidence_score=confidence,
                    import_id=import_id,
                    verification_status=record.verification_status,
                    is_synthetic=record.source_type == "demo",
                )
            )
        db.flush()
        if len(result.entries) < _SAMPLE_LIMIT:
            result.entries.append(
                PlanEntry(
                    row=i,
                    action=action,
                    product_external_id=record.product_external_id,
                    store_external_code=record.store_external_code,
                    amount=str(record.amount),
                )
            )
    return result


# --------------------------------------------------------------------------- #
# Orchestration: create (validate + dry-run/preview), commit, rollback
# --------------------------------------------------------------------------- #
def _adapter_for_format(fmt: str):
    if fmt == "csv":
        return CsvRetailerAdapter()
    if fmt == "json":
        return JsonRetailerAdapter()
    raise ValueError(f"formato no soportado: {fmt}")


def _single_or_none(values: set[str | None]) -> str | None:
    real = {v for v in values if v}
    return next(iter(real)) if len(real) == 1 else None


def create_import(
    db: Session,
    *,
    content: str | bytes,
    fmt: str,
    filename: str | None = None,
    mapping: dict[str, str] | None = None,
    dry_run: bool = True,
    user_id: int | None = None,
    ip: str | None = None,
) -> DataImport:
    """Parse + validate a file, compute what a commit *would* change, persist a DataImport.

    Writes NO ``Product``/``ProductPrice`` rows. Structured per-row errors, aggregate
    stats and the validated records are stored in ``summary`` (the records let a later
    ``commit_import`` run without re-uploading the file). ``status`` is ``dry_run`` when
    ``dry_run`` is set, otherwise ``pending`` (validated, awaiting commit).
    """
    adapter = _adapter_for_format(fmt)
    raw_bytes = content.encode("utf-8") if isinstance(content, str) else content
    checksum = hashlib.sha256(raw_bytes).hexdigest()
    parsed = adapter.parse(content, mapping)

    errors: list[RowError] = [
        RowError(row=e.row, field=None, message=e.message) for e in parsed.errors
    ]
    records: list[NormalizedRecord] = []
    seen_obs: set[tuple[str, str, str, str]] = set()
    error_rows = 0

    for index, raw in enumerate(parsed.rows, start=1):
        validation = build_record(raw, index)
        if validation.errors:
            errors.extend(validation.errors)
            error_rows += 1
            continue
        record = validation.record
        assert record is not None
        key = _obs_key(record)
        if key in seen_obs:
            errors.append(
                RowError(row=index, field=None, message="fila duplicada en el lote (omitida)")
            )
            continue
        seen_obs.add(key)
        records.append(record)

    plan = plan_changes(db, records)
    row_count = len(parsed.rows)
    duplicate_count = len(parsed.rows) - error_rows - len(records)
    ok_count = row_count - error_rows
    skipped_count = plan.skipped + duplicate_count

    retailer_id = None
    slug = _single_or_none({r.retailer_slug for r in records})
    if slug:
        retailer = db.execute(
            select(Retailer).where(Retailer.slug == slug)
        ).scalar_one_or_none()
        retailer_id = retailer.id if retailer else None
    source_type = _single_or_none({r.source_type for r in records})

    data_source = db.execute(
        select(DataSource).where(DataSource.adapter_key == adapter.adapter_key)
    ).scalar_one_or_none()

    summary = {
        "stats": {
            "row_count": row_count,
            "ok_count": ok_count,
            "error_count": error_rows,
            "created": plan.created,
            "updated": plan.updated,
            "skipped": skipped_count,
            "duplicates_in_batch": duplicate_count,
        },
        "errors": [{"row": e.row, "field": e.field, "message": e.message} for e in errors],
        "sample": [
            {
                "row": e.row,
                "action": e.action,
                "product_external_id": e.product_external_id,
                "store_external_code": e.store_external_code,
                "amount": e.amount,
            }
            for e in plan.entries
        ],
        "records": [r.to_json() for r in records],
        "mapping": mapping or {},
        "format": fmt,
        "filename": filename,
        "adapter_key": adapter.adapter_key,
    }

    data_import = DataImport(
        retailer_id=retailer_id,
        data_source_id=data_source.id if data_source else None,
        source_type=source_type,
        status="dry_run" if dry_run else "pending",
        filename=filename,
        format=fmt,
        checksum=checksum,
        row_count=row_count,
        ok_count=ok_count,
        error_count=error_rows,
        created_count=plan.created,
        updated_count=plan.updated,
        skipped_count=skipped_count,
        dry_run=dry_run,
        summary=summary,
        created_by_user_id=user_id,
    )
    db.add(data_import)
    db.flush()

    record_audit(
        db,
        action="data_import.dry_run" if dry_run else "data_import.create",
        actor_user_id=user_id,
        entity_type="data_import",
        entity_public_id=data_import.public_id,
        metadata={"filename": filename, "rows": row_count, "errors": error_rows},
        ip=ip,
    )
    return data_import


def commit_import(
    db: Session, data_import: DataImport, *, user_id: int | None = None, ip: str | None = None
) -> DataImport:
    """Apply a previously validated import: write products/prices tagged with its id.

    Only ``pending`` or ``dry_run`` imports can be committed; committing anything else (a
    committed or rolled-back batch) raises ``ValueError``. The validated records come from
    ``summary['records']`` — the file is not needed again.
    """
    if data_import.status not in ("pending", "dry_run"):
        raise ValueError(
            f"no se puede confirmar una importación en estado '{data_import.status}'"
        )
    summary = data_import.summary or {}
    records = [NormalizedRecord.from_json(r) for r in summary.get("records", [])]

    now = datetime.now(UTC)
    plan = commit_records(db, records, import_id=data_import.id, now=now)

    data_import.status = "committed"
    data_import.dry_run = False
    data_import.committed_at = now
    data_import.created_count = plan.created
    data_import.updated_count = plan.updated
    # Preserve in-batch duplicates already counted at creation time.
    duplicate_count = int(summary.get("stats", {}).get("duplicates_in_batch", 0) or 0)
    data_import.skipped_count = plan.skipped + duplicate_count
    new_summary = dict(summary)
    new_summary["commit"] = {
        "created": plan.created,
        "updated": plan.updated,
        "skipped": plan.skipped,
        "committed_at": now.isoformat(),
    }
    data_import.summary = new_summary

    record_audit(
        db,
        action="data_import.commit",
        actor_user_id=user_id,
        entity_type="data_import",
        entity_public_id=data_import.public_id,
        metadata={"created": plan.created, "updated": plan.updated, "skipped": plan.skipped},
        ip=ip,
    )
    return data_import


def rollback_import(
    db: Session, data_import: DataImport, *, user_id: int | None = None, ip: str | None = None
) -> int:
    """Logically roll back a committed import.

    Deletes exactly the ``ProductPrice`` observations this batch created (matched by
    ``import_id``) and sets the batch's status to ``rolled_back``. It does NOT delete the
    products, stores, retailers or barcodes the import may have created — only the price
    observations are removed. Returns the number of price rows deleted.
    """
    if data_import.status != "committed":
        raise ValueError(
            f"sólo se puede revertir una importación 'committed' (estado actual: "
            f"'{data_import.status}')"
        )
    deleted = db.execute(
        select(func.count(ProductPrice.id)).where(
            ProductPrice.import_id == data_import.id
        )
    ).scalar_one()
    db.execute(delete(ProductPrice).where(ProductPrice.import_id == data_import.id))

    now = datetime.now(UTC)
    data_import.status = "rolled_back"
    data_import.rolled_back_at = now
    new_summary = dict(data_import.summary or {})
    new_summary["rollback"] = {"deleted_prices": deleted, "rolled_back_at": now.isoformat()}
    data_import.summary = new_summary

    record_audit(
        db,
        action="data_import.rollback",
        actor_user_id=user_id,
        entity_type="data_import",
        entity_public_id=data_import.public_id,
        metadata={"deleted_prices": deleted},
        ip=ip,
    )
    return deleted
