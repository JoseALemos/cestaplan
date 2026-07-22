"""Product enrichment from Open Food Facts (OFF) — data only, **never prices**.

This service fetches product data from OFF (via :class:`OpenFoodFactsAdapter`) and, when
asked to apply, idempotently writes it onto the matching ``Product``: barcode, nutrition,
declared allergens/traces, ingredients, brand, image reference and category. It **never**
reads or writes a price and never touches ``ProductPrice``.

Design guarantees (docs/DATA_SOURCES.md sections 3-5):

- **Gated.** Everything runs behind the OFF ``DataSource.is_enabled`` flag. If OFF is
  disabled the operation refuses (``status='disabled'`` → the endpoint answers 409).
- **Graceful.** A not-found / unavailable OFF lookup yields ``status='not_found'`` with no
  writes and no fabricated data.
- **Idempotent.** ``ProductBarcode`` and ``ProductNutrition`` are upserted, so applying the
  same enrichment twice creates no duplicates.
- **No matching product.** When enriching *by barcode* and no ``Product`` carries that
  barcode, the safer documented behaviour is taken: **nothing is created** and the result is
  ``status='no_product'``. Minimal price-less products are never injected into the catalogue
  from a bare barcode; catalogue products are only ever created by an authenticated admin
  import (``admin_import``).
- **Provenance.** Written nutrition carries ``source_type='open_dataset'`` and OFF's
  ``source_url``; ODbL attribution is surfaced on every result.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from cestaplan_api.adapters.openfoodfacts import (
    OFF_ADAPTER_KEY,
    OFF_API_BASE,
    OFF_ATTRIBUTION_TEXT,
    OFF_DATA_SOURCE_SLUG,
    OFF_LICENSE_CODE,
    OffProduct,
    OpenFoodFactsAdapter,
)
from cestaplan_api.models import DataSource, Product, ProductBarcode, ProductNutrition


@dataclass(slots=True)
class EnrichmentResult:
    """Outcome of an enrichment request.

    ``status`` is one of: ``found`` (dry lookup succeeded), ``applied`` (data written to a
    product), ``not_found`` (OFF had no product / was unavailable), ``no_product`` (OFF had
    data but no catalogue product carries the barcode), ``no_barcode`` (a product without any
    barcode and none supplied) or ``disabled`` (OFF source turned off).
    """

    status: str
    barcode: str
    attribution: str = OFF_ATTRIBUTION_TEXT
    license_code: str = OFF_LICENSE_CODE
    source_url: str | None = None
    product: dict[str, Any] | None = None
    applied: bool = False
    product_public_id: str | None = None
    matched_products: int = 0
    message: str | None = None

    @property
    def found(self) -> bool:
        return self.status in ("found", "applied")


# --------------------------------------------------------------------------- #
# OFF DataSource row (ODbL) — ensured to exist, gated by is_enabled
# --------------------------------------------------------------------------- #
def ensure_off_data_source(db: Session) -> DataSource:
    """Return the OFF ``DataSource`` row, creating it (enabled) if it does not exist yet.

    Never overwrites an existing row's ``is_enabled`` — an admin who disabled OFF stays in
    control. Creating this config row is the only write a *dry* lookup performs.
    """
    ds = db.execute(
        select(DataSource).where(DataSource.adapter_key == OFF_ADAPTER_KEY)
    ).scalar_one_or_none()
    if ds is None:
        ds = DataSource(
            slug=OFF_DATA_SOURCE_SLUG,
            name="Open Food Facts",
            source_type="open_dataset",
            adapter_key=OFF_ADAPTER_KEY,
            license_code=OFF_LICENSE_CODE,
            attribution_text=OFF_ATTRIBUTION_TEXT,
            is_enabled=True,
            url=OFF_API_BASE,
        )
        db.add(ds)
        db.flush()
    return ds


def off_source_enabled(db: Session) -> bool:
    """Whether the OFF source is enabled (ensuring its row exists first)."""
    return ensure_off_data_source(db).is_enabled


# --------------------------------------------------------------------------- #
# Persistence helpers (data only — prices are never touched)
# --------------------------------------------------------------------------- #
def _ensure_barcode(db: Session, product: Product, barcode: str) -> None:
    """Attach ``barcode`` to ``product`` once (idempotent; first barcode becomes primary)."""
    exists = db.execute(
        select(ProductBarcode.id).where(
            ProductBarcode.product_id == product.id,
            ProductBarcode.barcode == barcode,
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
            barcode=barcode,
            is_primary=has_any is None,
        )
    )


def _upsert_nutrition(db: Session, product: Product, off: OffProduct) -> None:
    """Create or update the 1:1 ``ProductNutrition`` for ``product`` from OFF data.

    Upsert (not insert) so re-running never violates the unique ``(product_id)`` constraint
    nor duplicates rows. Nutrition basis is per 100 g/ml (OFF's ``*_100g`` fields).
    """
    nutrition = db.execute(
        select(ProductNutrition).where(ProductNutrition.product_id == product.id)
    ).scalar_one_or_none()
    if nutrition is None:
        nutrition = ProductNutrition(
            product_id=product.id,
            basis_quantity=Decimal("100"),
            basis_unit="g",
        )
        db.add(nutrition)

    nutrition.basis_quantity = Decimal("100")
    nutrition.basis_unit = "g"
    nutrition.energy_kcal = off.energy_kcal
    nutrition.protein_g = off.protein_g
    nutrition.carbohydrate_g = off.carbohydrate_g
    nutrition.sugars_g = off.sugars_g
    nutrition.fat_g = off.fat_g
    nutrition.saturated_fat_g = off.saturated_fat_g
    nutrition.fiber_g = off.fiber_g
    nutrition.salt_g = off.salt_g
    nutrition.allergens = list(off.allergens) if off.allergens else None
    nutrition.traces = list(off.traces) if off.traces else None
    if off.ingredients_text is not None:
        nutrition.ingredients_text = off.ingredients_text
    nutrition.source_type = "open_dataset"
    nutrition.source_url = off.source_url
    nutrition.is_synthetic = False


def _apply_to_product(db: Session, product: Product, off: OffProduct) -> None:
    """Write OFF product data onto ``product``. Never reads or writes any price."""
    _ensure_barcode(db, product, off.barcode)
    # Non-destructive catalogue fields: only overwrite when OFF actually provides a value.
    if off.brands:
        product.brand = off.brands
    if off.image_url:
        product.image_url = off.image_url  # reference/URL only, per ODbL image handling.
    if off.category_code:
        product.category_code = off.category_code
    _upsert_nutrition(db, product, off)
    db.flush()


def _products_for_barcode(db: Session, barcode: str) -> list[Product]:
    return list(
        db.execute(
            select(Product)
            .join(ProductBarcode, ProductBarcode.product_id == Product.id)
            .where(ProductBarcode.barcode == barcode, Product.deleted_at.is_(None))
        )
        .scalars()
        .all()
    )


def _result_for(off: OffProduct, status: str, **kwargs: Any) -> EnrichmentResult:
    return EnrichmentResult(
        status=status,
        barcode=off.barcode,
        source_url=off.source_url,
        product=off.to_public_dict(),
        **kwargs,
    )


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def enrich_product_by_barcode(
    db: Session,
    barcode: str,
    *,
    apply: bool,
    adapter: OpenFoodFactsAdapter | None = None,
) -> EnrichmentResult:
    """Look ``barcode`` up on OFF and, when ``apply`` is set, write it to matching products.

    With ``apply=False`` this is a pure lookup (no product writes). With ``apply=True`` the
    OFF data is upserted onto every ``Product`` that carries ``barcode``; if none does, the
    result is ``status='no_product'`` and nothing is written (see module docstring). Prices
    are never read or written.
    """
    code = (barcode or "").strip()
    if not off_source_enabled(db):
        return EnrichmentResult(
            status="disabled",
            barcode=code,
            message="La fuente Open Food Facts está deshabilitada",
        )

    adapter = adapter or OpenFoodFactsAdapter()
    off = adapter.fetch_by_barcode(code)
    if off is None:
        return EnrichmentResult(
            status="not_found",
            barcode=code,
            message="Producto no encontrado en Open Food Facts o fuente no disponible",
        )

    if not apply:
        return _result_for(off, "found")

    products = _products_for_barcode(db, off.barcode)
    if not products:
        return _result_for(
            off,
            "no_product",
            message="Ningún producto del catálogo tiene ese código de barras",
        )

    for product in products:
        _apply_to_product(db, product, off)

    return _result_for(
        off,
        "applied",
        applied=True,
        product_public_id=str(products[0].public_id),
        matched_products=len(products),
    )


def enrich_product(
    db: Session,
    product: Product,
    *,
    barcode: str | None = None,
    apply: bool = True,
    adapter: OpenFoodFactsAdapter | None = None,
) -> EnrichmentResult:
    """Enrich one specific ``product`` from OFF, resolving its barcode.

    Uses ``barcode`` when supplied, otherwise the product's primary/first
    :class:`ProductBarcode`. Returns ``status='no_barcode'`` when neither is available.
    """
    if not off_source_enabled(db):
        return EnrichmentResult(
            status="disabled",
            barcode=barcode or "",
            message="La fuente Open Food Facts está deshabilitada",
        )

    code = (barcode or "").strip()
    if not code:
        existing = db.execute(
            select(ProductBarcode)
            .where(ProductBarcode.product_id == product.id)
            .order_by(ProductBarcode.is_primary.desc(), ProductBarcode.id.asc())
        ).scalars().first()
        code = existing.barcode if existing else ""
    if not code:
        return EnrichmentResult(
            status="no_barcode",
            barcode="",
            product_public_id=str(product.public_id),
            message="El producto no tiene código de barras y no se aportó ninguno",
        )

    adapter = adapter or OpenFoodFactsAdapter()
    off = adapter.fetch_by_barcode(code)
    if off is None:
        return EnrichmentResult(
            status="not_found",
            barcode=code,
            product_public_id=str(product.public_id),
            message="Producto no encontrado en Open Food Facts o fuente no disponible",
        )

    if not apply:
        return _result_for(off, "found", product_public_id=str(product.public_id))

    _apply_to_product(db, product, off)
    return _result_for(
        off,
        "applied",
        applied=True,
        product_public_id=str(product.public_id),
        matched_products=1,
    )
