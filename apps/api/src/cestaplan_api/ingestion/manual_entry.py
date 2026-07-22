"""Manual price entry for the price-ingestion subsystem (spec §17, FASE E).

:func:`record_manual_price` is the honest, operator-driven counterpart to the automated
connectors: an admin types in a price they observed (a shelf ticket, a phone call to a store),
and it is recorded as a first-class :class:`PriceObservation` with ``price_type=manual`` — never
fabricated, never guessed. It reuses the exact same append-only history machinery as every
connector (:func:`~cestaplan_api.ingestion.price_history.record_observation`) so a manual price
has full provenance, is projected to the meal-plan engine, and is audited:

- The amount is validated (a positive :class:`~decimal.Decimal`) and the currency is checked; a
  missing/zero/negative amount is rejected, never coerced to ``0``.
- Scope is honest: ``exact_store`` is only claimed when a concrete store is supplied, otherwise
  the price is ``national`` (a manual national reference). Claiming ``exact_store`` without a
  store raises.
- It links a ``manual`` :class:`~cestaplan_api.models.DataSource` (``source_type=manual_entry``,
  legal footing ``authorized`` — operator-entered, not scraped) for traceability, projects the
  current price via :class:`~cestaplan_api.ingestion.current_price.CurrentPriceService`, and
  writes an :class:`~cestaplan_api.models.AuditLog` entry.

The caller owns the transaction — this flushes but never commits.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.orm import Session

from cestaplan_api.ingestion import (
    NormalizedObservation,
    PriceScope,
    PriceType,
    SourceRef,
)
from cestaplan_api.ingestion.current_price import CurrentPriceService
from cestaplan_api.ingestion.orchestration import _resolve_variant
from cestaplan_api.ingestion.price_history import record_observation
from cestaplan_api.models import (
    DataSource,
    ExternalProduct,
    PriceObservation,
    Product,
    ProductVariant,
    Retailer,
    Store,
)
from cestaplan_api.services.audit import record_audit

#: Confidence carried by an operator-typed manual price (mirrors the importer's manual midpoint).
_MANUAL_CONFIDENCE = Decimal("0.6500")

#: The single ``manual`` data source every manual entry is attributed to (created lazily).
_MANUAL_SOURCE_SLUG = "manual"
_MANUAL_SOURCE_NAME = "Entrada manual de precios"


class ManualPriceError(ValueError):
    """Raised when a manual price entry is invalid (bad amount/currency/scope/target)."""


def record_manual_price(
    db: Session,
    *,
    retailer: Retailer,
    amount: object,
    store: Store | None = None,
    product: Product | None = None,
    variant: ProductVariant | None = None,
    barcode: str | None = None,
    currency: str = "EUR",
    unit: str | None = None,
    price_scope: PriceScope | None = None,
    price_type: PriceType = PriceType.MANUAL,
    observed_at: datetime | None = None,
    note: str | None = None,
    user_id: int | None = None,
) -> PriceObservation:
    """Record an operator-typed price as a manual :class:`PriceObservation`.

    Resolves the target :class:`ProductVariant` from (in priority order) an explicit ``variant``,
    a ``product``, or a ``barcode``; validates the amount/currency; records the observation into
    the append-only history (``price_type=manual``); projects the current price; and audits it.
    """
    as_of = observed_at or datetime.now(UTC)
    money = _validate_amount(amount)
    code = _validate_currency(currency)
    scope = _resolve_scope(price_scope, store)
    resolved = _resolve_target_variant(
        db, retailer, variant=variant, product=product, barcode=barcode, as_of=as_of
    )
    source = _manual_data_source(db)

    observation = NormalizedObservation(
        variant_ref=str(resolved.id),
        amount=money,
        currency=code,
        price_scope=scope,
        price_type=price_type,
        observed_at=as_of,
        unit_amount=None,  # never fabricate a unit price from a bare manual amount
        unit_code=unit,
        promotion=None,
        requires_loyalty=False,
        available=None,
        confidence=_MANUAL_CONFIDENCE,
        source=SourceRef(
            source_slug=_MANUAL_SOURCE_SLUG,
            connector_version="manual",
            parser_version="manual",
        ),
    )
    row = record_observation(
        db,
        observation,
        product_variant_id=resolved.id,
        retailer_id=retailer.id,
        as_of=as_of,
        store_id=store.id if store is not None else None,
        source_id=source.id,
    )
    # Project into ProductPrice so the meal-plan engine sees the manual price (store-scoped only).
    CurrentPriceService().project_current_prices(db, retailer.id)
    record_audit(
        db,
        action="price.manual_entry",
        actor_user_id=user_id,
        entity_type="price_observation",
        entity_public_id=row.public_id,
        metadata={
            "retailer": retailer.slug,
            "store_id": store.id if store is not None else None,
            "product_variant_id": resolved.id,
            "amount": str(money),
            "currency": code,
            "price_scope": scope.value,
            "price_type": price_type.value,
            "note": note,
        },
    )
    return row


# --------------------------------------------------------------------------- #
# Validation & resolution
# --------------------------------------------------------------------------- #
def _validate_amount(amount: object) -> Decimal:
    """Coerce ``amount`` to a positive :class:`Decimal` (never through ``float``); reject <= 0."""
    if isinstance(amount, Decimal):
        money = amount
    elif isinstance(amount, (int, str)):
        try:
            money = Decimal(str(amount).strip().replace(",", "."))
        except InvalidOperation as exc:
            raise ManualPriceError(f"amount is not a valid number: {amount!r}") from exc
    else:
        raise ManualPriceError(f"amount is not a valid number: {amount!r}")
    if money <= 0:
        raise ManualPriceError("amount must be > 0")
    return money


def _validate_currency(currency: str) -> str:
    code = (currency or "").strip().upper()
    if code != "EUR":
        raise ManualPriceError(f"unsupported currency: {currency!r}")
    return code


def _resolve_scope(price_scope: PriceScope | None, store: Store | None) -> PriceScope:
    """Honest scope: ``exact_store`` only with a store; store-less defaults to ``national``."""
    if store is None:
        if price_scope is PriceScope.EXACT_STORE:
            raise ManualPriceError("exact_store scope requires a store")
        return price_scope or PriceScope.NATIONAL
    return price_scope or PriceScope.EXACT_STORE


def _resolve_target_variant(
    db: Session,
    retailer: Retailer,
    *,
    variant: ProductVariant | None,
    product: Product | None,
    barcode: str | None,
    as_of: datetime,
) -> ProductVariant:
    """Resolve the variant a manual price is for, upserting one for a product/barcode if needed."""
    if variant is not None:
        return variant
    if product is not None:
        return _variant_for_product(db, retailer, product, as_of=as_of)
    if barcode is not None and barcode.strip():
        external_id = barcode.strip()
        # Reuse the pipeline's idempotent ExternalProduct/Product/ProductVariant upsert.
        seed = NormalizedObservation(
            variant_ref=external_id,
            amount=Decimal("0"),
            currency="EUR",
            price_scope=PriceScope.NATIONAL,
            price_type=PriceType.MANUAL,
            observed_at=as_of,
            source=SourceRef(source_slug=_MANUAL_SOURCE_SLUG),
        )
        raw: dict[str, object] = {
            "external_id": external_id,
            "name": external_id,
            "barcode": barcode,
        }
        return _resolve_variant(db, retailer.id, seed, raw, as_of=as_of)
    raise ManualPriceError("a manual price requires a variant, product or barcode")


def _variant_for_product(
    db: Session, retailer: Retailer, product: Product, *, as_of: datetime
) -> ProductVariant:
    """Find or create the retailer's :class:`ProductVariant` for an existing canonical product."""
    existing = db.execute(
        select(ProductVariant).where(
            ProductVariant.product_id == product.id,
            ProductVariant.retailer_id == retailer.id,
        )
    ).scalars().first()
    if existing is not None:
        return existing

    external_id = f"manual:{product.id}"
    external = db.execute(
        select(ExternalProduct).where(
            ExternalProduct.retailer_id == retailer.id,
            ExternalProduct.external_id == external_id,
        )
    ).scalars().first()
    if external is None:
        external = ExternalProduct(
            retailer_id=retailer.id,
            external_id=external_id,
            canonical_product_id=product.id,
            first_seen_at=as_of,
            last_seen_at=as_of,
            active=True,
        )
        db.add(external)
        db.flush()

    variant = ProductVariant(
        product_id=product.id,
        retailer_id=retailer.id,
        external_product_id=external.id,
        display_name=product.name,
        package_quantity=product.package_quantity,
        package_unit=product.package_unit,
        package_count=1,
        active=True,
    )
    db.add(variant)
    db.flush()
    return variant


def _manual_data_source(db: Session) -> DataSource:
    """Get (or lazily create) the single ``manual`` :class:`DataSource`."""
    source = db.execute(
        select(DataSource).where(DataSource.slug == _MANUAL_SOURCE_SLUG)
    ).scalar_one_or_none()
    if source is not None:
        return source
    source = DataSource(
        slug=_MANUAL_SOURCE_SLUG,
        name=_MANUAL_SOURCE_NAME,
        source_type="manual_entry",
        legal_status="authorized",
        is_enabled=True,
    )
    db.add(source)
    db.flush()
    return source


__all__ = ["ManualPriceError", "record_manual_price"]
