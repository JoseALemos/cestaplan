"""Provider-agnostic licensed-catalog import (FASE 2).

A licensed feed/catalogue arrives in the *supplier's* own shape. This module turns it into
our product/price contract without hardcoding any supplier schema:

- :class:`SupplierFieldMap` (the plain-data view of a
  :class:`~cestaplan_api.models.ingestion.SupplierFieldMapping`) declares which supplier
  field feeds each of our fields (dotted paths allowed) plus unit aliases.
- :func:`resolve_record` applies that map to one supplier payload row, coercing money to
  :class:`~decimal.Decimal` and normalizing units — never guessing, never using ``float``.
- :class:`CsvLicensedCatalogImporter` / :class:`JsonLicensedCatalogImporter` parse the raw
  bytes into rows and hand each to :func:`resolve_record`.
- :func:`persist_records` upserts ``ExternalProduct``/``Product``/``ProductVariant`` (with
  net content) and appends an append-only ``PriceObservation``, idempotently, with a
  ``dry_run`` that computes the outcome and writes nothing.

Deliberately, nothing here resolves ``canonical_name``: the recipe-ingredient link belongs
to the internal taxonomy and is produced by the separate mapping/review pipeline (FASE 3/4).
"""

from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.orm import Session

from cestaplan_api.ingestion.contracts import PriceScope, PriceType
from cestaplan_api.models import (
    DataSource,
    ExternalProduct,
    PriceObservation,
    Product,
    ProductVariant,
    Retailer,
    Store,
)
from cestaplan_api.models.ingestion import SELL_UNITS

# Built-in unit vocabulary (extended per-supplier by SupplierFieldMapping.unit_aliases).
_BUILTIN_UNIT_ALIASES: dict[str, str] = {
    "g": "g", "gr": "g", "gs": "g", "gramo": "g", "gramos": "g", "gram": "g", "grams": "g",
    "kg": "kg", "kgs": "kg", "kilo": "kg", "kilos": "kg", "kilogramo": "kg",
    "kilogramos": "kg", "kilogram": "kg",
    "mg": "mg",
    "ml": "ml", "mililitro": "ml", "mililitros": "ml", "milliliter": "ml",
    "l": "l", "lt": "l", "lts": "l", "litro": "l", "litros": "l", "liter": "l", "litre": "l",
    "cl": "cl", "centilitro": "cl",
    "unit": "unit", "units": "unit", "ud": "unit", "uds": "unit", "u": "unit",
    "unidad": "unit", "unidades": "unit", "pieza": "unit", "piece": "unit", "pcs": "unit",
}
# Mass/volume units the planner can cost a recipe against.
COSTABLE_UNITS = frozenset({"g", "kg", "mg", "ml", "l", "cl"})


@dataclass(frozen=True, slots=True)
class SupplierFieldMap:
    """Plain-data view of a ``SupplierFieldMapping`` row (keeps this module pure/testable)."""

    field_map: dict[str, str]
    unit_aliases: dict[str, str] = field(default_factory=dict)
    default_currency: str | None = None


@dataclass(slots=True)
class LicensedRecord:
    """One supplier row resolved into our product + price contract."""

    external_id: str
    product_name: str
    amount: Decimal
    currency: str
    brand: str | None = None
    barcode: str | None = None
    category: str | None = None
    sell_unit: str | None = None
    package_quantity: Decimal | None = None
    package_unit: str | None = None
    net_content_quantity: Decimal | None = None
    net_content_unit: str | None = None
    variable_weight: bool = False
    unit_price: Decimal | None = None
    unit_price_unit: str | None = None
    store_external_id: str | None = None
    postal_code: str | None = None
    province: str | None = None
    locality: str | None = None
    observed_at: datetime | None = None
    source_url: str | None = None


@dataclass(slots=True)
class RowError:
    """A row that could not be resolved, kept for the error report (never silently dropped)."""

    row_index: int
    errors: list[str]
    external_id: str | None = None


@dataclass(slots=True)
class PersistOutcome:
    """Counts from persisting (or dry-running) a batch of licensed records."""

    dry_run: bool = False
    products_created: int = 0
    variants_created: int = 0
    variants_updated: int = 0
    observations_inserted: int = 0
    observations_skipped: int = 0
    costable_variants: int = 0

    def as_dict(self) -> dict[str, object]:
        return {
            "dry_run": self.dry_run,
            "products_created": self.products_created,
            "variants_created": self.variants_created,
            "variants_updated": self.variants_updated,
            "observations_inserted": self.observations_inserted,
            "observations_skipped": self.observations_skipped,
            "costable_variants": self.costable_variants,
        }


# --------------------------------------------------------------------------- #
# Field resolution (SupplierFieldMapping application)
# --------------------------------------------------------------------------- #
def _get_path(payload: object, dotted: str) -> object:
    """Resolve a dotted path (``a.b.c``) against nested dicts/lists; None if absent."""
    current = payload
    for part in dotted.split("."):
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list) and part.isdigit():
            idx = int(part)
            current = current[idx] if 0 <= idx < len(current) else None
        else:
            return None
        if current is None:
            return None
    return current


def _to_decimal(raw: object) -> Decimal | None:
    """Parse money/quantity to Decimal, tolerating comma decimals and currency noise.

    Never uses float. Returns None when the value is absent or unparseable.
    """
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, Decimal):
        return raw
    if isinstance(raw, int):
        return Decimal(raw)
    text = str(raw).strip()
    if not text:
        return None
    # Drop currency symbols / spaces / thousands separators, keep digits, sign, separators.
    cleaned = "".join(c for c in text if c.isdigit() or c in ",.-")
    if not cleaned:
        return None
    if "," in cleaned and "." in cleaned:
        # Assume the last separator is the decimal point; the other is thousands.
        if cleaned.rfind(",") > cleaned.rfind("."):
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    elif "," in cleaned:
        cleaned = cleaned.replace(",", ".")
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def _to_bool(raw: object) -> bool:
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in {"1", "true", "t", "yes", "y", "si", "sí"}


def normalize_unit(raw: object, aliases: dict[str, str]) -> str | None:
    """Map a supplier unit string to our canonical unit code, or None if unknown/absent."""
    if raw is None:
        return None
    key = str(raw).strip().lower()
    if not key:
        return None
    return aliases.get(key) or _BUILTIN_UNIT_ALIASES.get(key)


def _str(raw: object) -> str | None:
    if raw is None:
        return None
    value = str(raw).strip()
    return value or None


def resolve_record(
    payload: dict[str, object], mapping: SupplierFieldMap
) -> tuple[LicensedRecord | None, list[str]]:
    """Resolve one supplier payload row into a :class:`LicensedRecord`.

    Returns ``(record, [])`` on success or ``(None, errors)`` when a required field
    (external_id, product_name, a positive amount, currency) is missing/invalid.
    """
    fm = mapping.field_map
    aliases = mapping.unit_aliases or {}

    def val(our_field: str) -> object:
        path = fm.get(our_field)
        return _get_path(payload, path) if path else None

    errors: list[str] = []
    external_id = _str(val("external_id"))
    product_name = _str(val("product_name"))
    amount = _to_decimal(val("amount"))
    currency = _str(val("currency")) or mapping.default_currency

    if not external_id:
        errors.append("missing external_id")
    if not product_name:
        errors.append("missing product_name")
    if amount is None:
        errors.append("missing or unparseable amount")
    elif amount <= 0:
        errors.append(f"non-positive amount {amount}")
    if not currency:
        errors.append("missing currency (no value and no default_currency)")

    sell_unit = _str(val("sell_unit"))
    if sell_unit is not None:
        sell_unit = sell_unit.lower()
        if sell_unit not in SELL_UNITS:
            errors.append(f"invalid sell_unit {sell_unit!r} (allowed: {', '.join(SELL_UNITS)})")

    if errors:
        return None, errors

    assert external_id is not None and product_name is not None
    assert amount is not None and currency is not None

    record = LicensedRecord(
        external_id=external_id,
        product_name=product_name,
        amount=amount,
        currency=currency.upper(),
        brand=_str(val("brand")),
        barcode=_str(val("barcode")),
        category=_str(val("category")),
        sell_unit=sell_unit,
        package_quantity=_to_decimal(val("package_quantity")),
        package_unit=normalize_unit(val("package_unit"), aliases),
        net_content_quantity=_to_decimal(val("net_content_quantity")),
        net_content_unit=normalize_unit(val("net_content_unit"), aliases),
        variable_weight=_to_bool(val("variable_weight")),
        unit_price=_to_decimal(val("unit_price")),
        unit_price_unit=normalize_unit(val("unit_price_unit"), aliases),
        store_external_id=_str(val("store_external_id")),
        postal_code=_str(val("postal_code")),
        province=_str(val("province")),
        locality=_str(val("locality")),
        source_url=_str(val("source_url")),
    )
    # Net content defaults to the package when the package itself is a mass/volume unit.
    if record.net_content_quantity is None and record.package_unit in COSTABLE_UNITS:
        record.net_content_quantity = record.package_quantity
        record.net_content_unit = record.package_unit
    return record, []


# --------------------------------------------------------------------------- #
# Importers (parse raw bytes -> rows -> records)
# --------------------------------------------------------------------------- #
class _BaseLicensedImporter:
    format_name = "base"

    def parse_rows(self, raw: bytes | str) -> list[dict[str, object]]:  # pragma: no cover
        raise NotImplementedError

    def to_records(
        self, raw: bytes | str, mapping: SupplierFieldMap
    ) -> tuple[list[LicensedRecord], list[RowError]]:
        """Parse and resolve every row; unresolved rows are reported, never dropped silently."""
        records: list[LicensedRecord] = []
        row_errors: list[RowError] = []
        for index, row in enumerate(self.parse_rows(raw)):
            record, errors = resolve_record(row, mapping)
            if record is not None:
                records.append(record)
            else:
                row_errors.append(
                    RowError(
                        row_index=index,
                        errors=errors,
                        external_id=_str(row.get(mapping.field_map.get("external_id", ""))),
                    )
                )
        return records, row_errors


class CsvLicensedCatalogImporter(_BaseLicensedImporter):
    """Parses a delimited licensed catalogue (header row + one product/price per line)."""

    format_name = "csv"

    def __init__(self, *, delimiter: str = ",") -> None:
        self._delimiter = delimiter

    def parse_rows(self, raw: bytes | str) -> list[dict[str, object]]:
        text = raw.decode("utf-8-sig") if isinstance(raw, bytes) else raw
        reader = csv.DictReader(io.StringIO(text), delimiter=self._delimiter)
        return [dict(row) for row in reader]


class JsonLicensedCatalogImporter(_BaseLicensedImporter):
    """Parses a JSON licensed catalogue: a top-level array or an object with an items path."""

    format_name = "json"

    def __init__(self, *, items_path: str | None = None) -> None:
        self._items_path = items_path

    def parse_rows(self, raw: bytes | str) -> list[dict[str, object]]:
        data = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
        items = _get_path(data, self._items_path) if self._items_path else data
        if not isinstance(items, list):
            raise ValueError(
                "JSON payload is not an array"
                + (f" at items_path {self._items_path!r}" if self._items_path else "")
            )
        return [row for row in items if isinstance(row, dict)]


# --------------------------------------------------------------------------- #
# Persistence (upsert variant + append price observation)
# --------------------------------------------------------------------------- #
def _now() -> datetime:
    return datetime.now(UTC)


def _resolve_store_id(db: Session, retailer_id: int, record: LicensedRecord) -> int | None:
    """Resolve an existing store by external code; None (chain-wide) when absent/unknown."""
    if not record.store_external_id:
        return None
    store = db.execute(
        select(Store).where(
            Store.retailer_id == retailer_id,
            Store.external_code == record.store_external_id,
        )
    ).scalars().first()
    return store.id if store is not None else None


def _upsert_variant(
    db: Session, retailer_id: int, record: LicensedRecord, outcome: PersistOutcome
) -> ProductVariant:
    external = db.execute(
        select(ExternalProduct).where(
            ExternalProduct.retailer_id == retailer_id,
            ExternalProduct.external_id == record.external_id,
        )
    ).scalars().first()
    now = _now()
    if external is None:
        external = ExternalProduct(
            retailer_id=retailer_id,
            external_id=record.external_id,
            external_url=record.source_url,
            first_seen_at=now,
            last_seen_at=now,
            active=True,
        )
        db.add(external)
        db.flush()
    else:
        external.last_seen_at = now

    if external.canonical_product_id is None:
        product = Product(
            retailer_id=retailer_id,
            external_id=record.external_id,
            name=record.product_name,
            brand=record.brand,
            package_quantity=record.package_quantity,
            package_unit=record.package_unit,
            category_code=record.category,
            is_synthetic=False,
        )
        db.add(product)
        db.flush()
        external.canonical_product_id = product.id
        db.flush()
        outcome.products_created += 1

    variant = db.execute(
        select(ProductVariant).where(
            ProductVariant.retailer_id == retailer_id,
            ProductVariant.external_product_id == external.id,
        )
    ).scalars().first()
    if variant is None:
        variant = ProductVariant(
            product_id=external.canonical_product_id,
            retailer_id=retailer_id,
            external_product_id=external.id,
            display_name=record.product_name,
            sell_unit=record.sell_unit,
            package_quantity=record.package_quantity,
            package_unit=record.package_unit,
            net_content_quantity=record.net_content_quantity,
            net_content_unit=record.net_content_unit,
            variable_weight=record.variable_weight,
            unit_price=record.unit_price,
            unit_price_unit=record.unit_price_unit,
            active=True,
        )
        db.add(variant)
        db.flush()
        outcome.variants_created += 1
    else:
        variant.display_name = record.product_name
        variant.sell_unit = record.sell_unit
        variant.package_quantity = record.package_quantity
        variant.package_unit = record.package_unit
        variant.net_content_quantity = record.net_content_quantity
        variant.net_content_unit = record.net_content_unit
        variant.variable_weight = record.variable_weight
        variant.unit_price = record.unit_price
        variant.unit_price_unit = record.unit_price_unit
        outcome.variants_updated += 1

    if record.net_content_unit in COSTABLE_UNITS and record.net_content_quantity:
        outcome.costable_variants += 1
    return variant


def _append_observation(
    db: Session,
    retailer_id: int,
    store_id: int | None,
    variant: ProductVariant,
    record: LicensedRecord,
    source: DataSource | None,
    as_of: datetime,
    outcome: PersistOutcome,
) -> None:
    observed_at = record.observed_at or as_of
    scope = PriceScope.EXACT_STORE if store_id is not None else PriceScope.NATIONAL

    # The latest still-open observation for this variant+scope+store.
    prior = db.execute(
        select(PriceObservation)
        .where(
            PriceObservation.product_variant_id == variant.id,
            PriceObservation.store_id == store_id,
            PriceObservation.price_scope == scope.value,
            PriceObservation.valid_until.is_(None),
        )
        .order_by(PriceObservation.valid_from.desc())
    ).scalars().first()

    # Idempotent + append-only: if the current price is unchanged, re-importing is a no-op;
    # a changed price appends a new row and closes the prior one (price history).
    if (
        prior is not None
        and prior.amount == record.amount
        and prior.currency == record.currency
    ):
        outcome.observations_skipped += 1
        return
    if prior is not None and prior.valid_from <= observed_at:
        prior.valid_until = observed_at

    db.add(
        PriceObservation(
            retailer_id=retailer_id,
            store_id=store_id,
            product_variant_id=variant.id,
            price_scope=scope.value,
            price_type=PriceType.REGULAR.value,
            amount=record.amount,
            currency=record.currency,
            unit_amount=record.unit_price,
            unit_code=record.unit_price_unit,
            available=True,
            source_id=source.id if source is not None else None,
            source_url=record.source_url,
            observed_at=observed_at,
            imported_at=as_of,
            valid_from=observed_at,
            confidence_score=Decimal("1.0"),
        )
    )
    db.flush()
    outcome.observations_inserted += 1


def persist_records(
    db: Session,
    retailer: Retailer,
    records: list[LicensedRecord],
    *,
    source: DataSource | None = None,
    dry_run: bool = False,
    as_of: datetime | None = None,
) -> PersistOutcome:
    """Upsert variants and append price observations for a batch of licensed records.

    Idempotent and append-only. With ``dry_run=True`` the work runs inside a savepoint that
    is rolled back, so the returned counts reflect what *would* happen without writing.
    """
    as_of = as_of or _now()
    outcome = PersistOutcome(dry_run=dry_run)
    savepoint = db.begin_nested()
    try:
        for record in records:
            store_id = _resolve_store_id(db, retailer.id, record)
            variant = _upsert_variant(db, retailer.id, record, outcome)
            _append_observation(
                db, retailer.id, store_id, variant, record, source, as_of, outcome
            )
    finally:
        if dry_run:
            savepoint.rollback()
        else:
            savepoint.commit()
    return outcome


__all__ = [
    "COSTABLE_UNITS",
    "CsvLicensedCatalogImporter",
    "JsonLicensedCatalogImporter",
    "LicensedRecord",
    "PersistOutcome",
    "RowError",
    "SupplierFieldMap",
    "normalize_unit",
    "persist_records",
    "resolve_record",
]
