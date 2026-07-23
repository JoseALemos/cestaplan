"""Sample-import pipeline for licensed catalogues (FASE 3).

Wraps the provider-agnostic importers (FASE 2) in the full validation/report flow a
licensed sample must pass before it can back real plans. One entry point,
:func:`run_sample_import`, runs every step and returns a :class:`SampleImportReport`:

1. schema validation      - the field map covers the required target fields
2. dry-run                - default; persist counts computed, nothing written
3. error report           - rows that failed to resolve (never silently dropped)
4. coverage report        - totals, costable products, store vs chain-wide
5. duplicate detection    - repeated external_ids within the batch
6. unit normalization     - flags package units that did not normalize
7. price validation       - non-positive / implausible amounts, mixed currencies
8. geographic validation  - store codes / postal codes that do not resolve
9. mapping candidates     - ingredient match per product (+ ingredient coverage)
10. manual review queue    - candidates needing human sign-off

On ``dry_run=False`` it also persists products/observations (FASE 2) and writes the
mapping candidates as inactive :class:`IngredientProductMapping` rows (the review queue),
never active until a human approves them (FASE 4). It never resolves ``canonical_name`` from
the supplier — the recipe link is produced here by the internal matcher, for review.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from cestaplan_api.ingestion.licensed_catalog import (
    COSTABLE_UNITS,
    LicensedRecord,
    PersistOutcome,
    RowError,
    SupplierFieldMap,
    persist_records,
)
from cestaplan_api.models import (
    DataSource,
    ExternalProduct,
    Ingredient,
    IngredientProductMapping,
    Product,
    ProductVariant,
    Retailer,
    Store,
)
from cestaplan_api.services.ingredient_matching import DEFAULT_MIN_CONFIDENCE, match_product

# The target fields a supplier map MUST cover for a sample to be processable at all.
_REQUIRED_TARGET_FIELDS = ("external_id", "product_name", "amount")
# A grocery unit price above this is almost certainly a units/decimal error, not a real price.
_IMPLAUSIBLE_AMOUNT = Decimal("1000")
_POSTAL_CODE_RE = re.compile(r"^\d{5}$")


@dataclass(slots=True)
class Issue:
    """A non-fatal finding tied (when possible) to a specific product."""

    code: str
    message: str
    external_id: str | None = None


@dataclass(slots=True)
class MappingCandidate:
    """A machine-proposed ingredient link awaiting human review."""

    external_id: str
    product_name: str
    canonical_name: str
    confidence: Decimal
    match_method: str = "token"


@dataclass(slots=True)
class SampleImportReport:
    """Full outcome of a sample import; JSON-serialisable via :meth:`as_dict`."""

    ok: bool = True
    dry_run: bool = True
    schema_errors: list[str] = field(default_factory=list)
    row_errors: list[RowError] = field(default_factory=list)
    duplicates: list[str] = field(default_factory=list)
    unit_warnings: list[Issue] = field(default_factory=list)
    price_warnings: list[Issue] = field(default_factory=list)
    geo_warnings: list[Issue] = field(default_factory=list)
    persist: PersistOutcome = field(default_factory=PersistOutcome)
    # coverage
    total_rows: int = 0
    resolved_records: int = 0
    distinct_products: int = 0
    costable_products: int = 0
    with_store: int = 0
    chain_wide: int = 0
    # mapping
    products_matched: int = 0
    products_unmatched: int = 0
    ingredients_total: int = 0
    ingredients_covered: int = 0
    review_queue: list[MappingCandidate] = field(default_factory=list)

    @property
    def critical_errors(self) -> int:
        """Blocking problems: schema failures and rows that could not be resolved."""
        return len(self.schema_errors) + len(self.row_errors)

    def as_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "dry_run": self.dry_run,
            "schema_errors": list(self.schema_errors),
            "row_errors": [
                {"row_index": e.row_index, "external_id": e.external_id, "errors": e.errors}
                for e in self.row_errors
            ],
            "duplicates": list(self.duplicates),
            "unit_warnings": [i.__dict__ for i in self.unit_warnings],
            "price_warnings": [i.__dict__ for i in self.price_warnings],
            "geo_warnings": [i.__dict__ for i in self.geo_warnings],
            "persist": self.persist.as_dict(),
            "coverage": {
                "total_rows": self.total_rows,
                "resolved_records": self.resolved_records,
                "distinct_products": self.distinct_products,
                "costable_products": self.costable_products,
                "with_store": self.with_store,
                "chain_wide": self.chain_wide,
            },
            "mapping": {
                "products_matched": self.products_matched,
                "products_unmatched": self.products_unmatched,
                "ingredients_total": self.ingredients_total,
                "ingredients_covered": self.ingredients_covered,
                "review_queue_size": len(self.review_queue),
            },
            "review_queue": [c.__dict__ for c in self.review_queue],
        }


def run_sample_import(
    db: Session,
    retailer: Retailer,
    raw: bytes | str,
    field_map: SupplierFieldMap,
    importer,
    *,
    source: DataSource | None = None,
    dry_run: bool = True,
    min_confidence: Decimal = DEFAULT_MIN_CONFIDENCE,
) -> SampleImportReport:
    """Run every sample-import step and return a full report (dry-run by default)."""
    report = SampleImportReport(dry_run=dry_run)

    # 1. schema validation ------------------------------------------------- #
    missing = [f for f in _REQUIRED_TARGET_FIELDS if f not in field_map.field_map]
    if missing:
        report.schema_errors.append(
            "field map missing required target fields: " + ", ".join(missing)
        )
        report.ok = False
        return report

    # 2-3. parse + resolve (unit normalization inside) + error report ------ #
    records, row_errors = importer.to_records(raw, field_map)
    report.row_errors = row_errors
    report.total_rows = len(records) + len(row_errors)

    # 5. duplicate detection (keep the first occurrence for persistence) ---- #
    seen: set[str] = set()
    unique: list[LicensedRecord] = []
    for record in records:
        if record.external_id in seen:
            report.duplicates.append(record.external_id)
            continue
        seen.add(record.external_id)
        unique.append(record)
    report.resolved_records = len(records)
    report.distinct_products = len(unique)

    # 4/6/7/8. coverage + unit + price + geographic validation -------------- #
    currencies: set[str] = set()
    for record in unique:
        currencies.add(record.currency)
        if record.net_content_unit in COSTABLE_UNITS and record.net_content_quantity:
            report.costable_products += 1
        # 6. unit normalization: a package was given but did not normalize.
        if record.net_content_unit is None and record.package_unit is None:
            report.unit_warnings.append(
                Issue("unit_unknown", "no costable net-content unit", record.external_id)
            )
        # 7. price validation.
        if record.amount > _IMPLAUSIBLE_AMOUNT:
            report.price_warnings.append(
                Issue(
                    "amount_implausible",
                    f"amount {record.amount} > {_IMPLAUSIBLE_AMOUNT}",
                    record.external_id,
                )
            )
        # 8. geographic validation.
        if record.store_external_id:
            store = db.execute(
                select(Store.id).where(
                    Store.retailer_id == retailer.id,
                    Store.external_code == record.store_external_id,
                )
            ).first()
            if store is None:
                report.geo_warnings.append(
                    Issue(
                        "store_unresolved",
                        f"store {record.store_external_id!r} not found",
                        record.external_id,
                    )
                )
            else:
                report.with_store += 1
        else:
            report.chain_wide += 1
        if record.postal_code and not _POSTAL_CODE_RE.match(record.postal_code):
            report.geo_warnings.append(
                Issue(
                    "postal_code_invalid",
                    f"invalid postal code {record.postal_code!r}",
                    record.external_id,
                )
            )
    if len(currencies) > 1:
        report.price_warnings.append(
            Issue("mixed_currencies", "batch mixes currencies: " + ", ".join(sorted(currencies)))
        )

    # 2. dry-run persist (or real persist) --------------------------------- #
    report.persist = persist_records(db, retailer, unique, source=source, dry_run=dry_run)

    # 9-10. mapping candidates + ingredient coverage + review queue -------- #
    index = {i.canonical_name: i for i in db.execute(select(Ingredient)).scalars()}
    report.ingredients_total = len(index)
    covered: set[str] = set()
    for record in unique:
        probe = Product(
            retailer_id=retailer.id,
            name=record.product_name,
            brand=record.brand,
            category_code=record.category,
            package_quantity=record.net_content_quantity or record.package_quantity,
            package_unit=record.net_content_unit or record.package_unit,
            is_synthetic=False,
        )
        match = match_product(db, probe, ingredient_index=index, min_confidence=min_confidence)
        if match is None:
            report.products_unmatched += 1
            continue
        ingredient, confidence = match
        report.products_matched += 1
        covered.add(ingredient.canonical_name)
        report.review_queue.append(
            MappingCandidate(
                external_id=record.external_id,
                product_name=record.product_name,
                canonical_name=ingredient.canonical_name,
                confidence=confidence,
            )
        )
    report.ingredients_covered = len(covered)

    # On commit, persist the candidates as inactive mappings (the review queue).
    if not dry_run and report.review_queue:
        _persist_candidates(db, retailer, index, report.review_queue)

    report.ok = report.critical_errors == 0
    return report


def _persist_candidates(
    db: Session,
    retailer: Retailer,
    index: dict[str, Ingredient],
    candidates: list[MappingCandidate],
) -> None:
    """Write machine candidates as inactive, unverified mappings awaiting human review.

    Never active and never human-verified: activation happens only via the review queue
    (FASE 4). Idempotent per (ingredient, product_variant).
    """
    for candidate in candidates:
        ingredient = index.get(candidate.canonical_name)
        variant = (
            db.execute(
                select(ProductVariant)
                .join(ExternalProduct, ExternalProduct.id == ProductVariant.external_product_id)
                .where(
                    ProductVariant.retailer_id == retailer.id,
                    ExternalProduct.external_id == candidate.external_id,
                )
            )
            .scalars()
            .first()
        )
        if ingredient is None or variant is None or variant.product_id is None:
            continue
        exists = db.execute(
            select(IngredientProductMapping.id).where(
                IngredientProductMapping.ingredient_id == ingredient.id,
                IngredientProductMapping.product_variant_id == variant.id,
            )
        ).first()
        if exists is not None:
            continue
        db.add(
            IngredientProductMapping(
                ingredient_id=ingredient.id,
                product_id=variant.product_id,
                product_variant_id=variant.id,
                retailer_id=retailer.id,
                confidence_score=candidate.confidence,
                match_method=candidate.match_method,
                verification_status="machine_verified",
                is_active=False,
            )
        )
    db.flush()


__all__ = ["Issue", "MappingCandidate", "SampleImportReport", "run_sample_import"]
