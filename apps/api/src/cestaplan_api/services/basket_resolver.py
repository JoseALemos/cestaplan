"""Basket resolution for the NutriPlan-facing PRICES API (FASE B, §19).

:func:`resolve_basket` turns a NutriPlan shopping request into a costed, honest basket:

For each requested item it (1) selects a concrete :class:`ProductVariant` (by variant id,
canonical product id, or ingredient name), (2) reads its **current price** scope-aware via
:class:`CurrentPriceService` (never fabricating one), (3) buys **whole packages** using the
engine's :func:`compute_packages` (never a fractional ``needed/pack*price``), and (4) applies
any structured :class:`PromotionRule` to the *number of packages bought* (e.g. a 2x1 charges
for ``ceil(n/2)``). Every line carries source, freshness/age, confidence and the leftover.

Items with no matching variant or no price are returned in :attr:`BasketResolution.unresolved`
— honestly, never priced with an invented number. The overall total is split into the cost
that comes from real observed prices (``known``) and from ``estimated`` observations.

Money and physical quantities are :class:`decimal.Decimal` throughout; the API layer is
responsible for serializing them to strings.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from cestaplan_api.ingestion.current_price import (
    CurrentPrice,
    CurrentPriceService,
    FreshnessStatus,
)
from cestaplan_api.models import (
    PriceObservation,
    Product,
    ProductVariant,
    PromotionRule,
)
from cestaplan_engine.packaging import compute_packages
from cestaplan_engine.units import ConversionError, UnitConverter

_ESTIMATED = "estimated"


# --------------------------------------------------------------------------- #
# Requested item + result value objects
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class BasketItem:
    """One requested line: a variant/product/ingredient plus how much is needed."""

    required_quantity: Decimal
    unit: str
    variant_id: uuid.UUID | None = None
    product_id: uuid.UUID | None = None
    ingredient: str | None = None


@dataclass(frozen=True, slots=True)
class PromotionApplied:
    """The structured promotion used to charge a line, plus its human description."""

    type: str
    description: str
    required_quantity: int | None
    charged_quantity: int | None
    percentage_discount: Decimal | None
    fixed_discount: Decimal | None


@dataclass(frozen=True, slots=True)
class ResolvedLine:
    """A costed basket line: the selected variant, whole-package math and provenance."""

    item: BasketItem
    variant_id: uuid.UUID
    product_id: uuid.UUID | None
    display_name: str
    required_quantity: Decimal
    required_unit: str
    package_quantity: Decimal
    package_unit: str
    packages: int
    purchased_quantity: Decimal
    used_quantity: Decimal
    leftover: Decimal
    unit_price: Decimal
    list_cost: Decimal
    line_cost: Decimal
    currency: str
    promotion: PromotionApplied | None
    price_type: str
    price_scope: str
    source_id: int | None
    observed_at: datetime
    age_seconds: Decimal
    freshness: FreshnessStatus
    confidence: Decimal
    available: bool | None

    @property
    def is_estimated(self) -> bool:
        return self.price_type == _ESTIMATED


@dataclass(frozen=True, slots=True)
class UnresolvedLine:
    """A requested item that could not be honestly priced, with the reason why."""

    item: BasketItem
    reason: str
    detail: str
    matched_variant_id: uuid.UUID | None = None


@dataclass(slots=True)
class BasketResolution:
    """The full costed basket: resolved lines, unresolved items and split totals."""

    retailer_id: uuid.UUID
    store_id: uuid.UUID | None
    as_of: datetime
    currency: str
    lines: list[ResolvedLine] = field(default_factory=list)
    unresolved: list[UnresolvedLine] = field(default_factory=list)

    @property
    def known_cost(self) -> Decimal:
        return sum(
            (line.line_cost for line in self.lines if not line.is_estimated),
            Decimal("0"),
        )

    @property
    def estimated_cost(self) -> Decimal:
        return sum(
            (line.line_cost for line in self.lines if line.is_estimated),
            Decimal("0"),
        )

    @property
    def total_cost(self) -> Decimal:
        return self.known_cost + self.estimated_cost

    @property
    def item_count(self) -> int:
        return len(self.lines) + len(self.unresolved)

    @property
    def coverage_ratio(self) -> Decimal:
        total = self.item_count
        if total == 0:
            return Decimal("0")
        return (Decimal(len(self.lines)) / Decimal(total)).quantize(Decimal("0.0001"))


# --------------------------------------------------------------------------- #
# Promotion evaluation
# --------------------------------------------------------------------------- #
def apply_promotion(
    rule: PromotionRule | None, packages: int, package_price: Decimal
) -> tuple[Decimal, PromotionApplied | None]:
    """Charge ``packages`` at ``package_price`` after applying ``rule`` (exact Decimal).

    Supported shapes (unknown/insufficiently-specified rules leave the price unchanged):
    - ``nxm`` (buy N pay M, e.g. 2x1): full groups pay ``M`` each, the remainder pays full.
    - ``second_unit``: every ``required`` units, one unit gets ``percentage_discount`` off.
    - ``percentage``: a percentage off, applied once the (optional) ``required`` is met.
    - ``fixed``: a fixed money discount off the whole line, floored at zero.
    """
    base = Decimal(packages) * package_price
    if rule is None or packages <= 0:
        return base, None

    ptype = rule.type
    applied: PromotionApplied | None = None
    cost = base

    def _mk(desc: str) -> PromotionApplied:
        return PromotionApplied(
            type=ptype,
            description=desc,
            required_quantity=rule.required_quantity,
            charged_quantity=rule.charged_quantity,
            percentage_discount=rule.percentage_discount,
            fixed_discount=rule.fixed_discount,
        )

    if ptype in {"nxm", "pack"} and rule.required_quantity and rule.charged_quantity:
        n = rule.required_quantity
        m = rule.charged_quantity
        paid_units = (packages // n) * m + (packages % n)
        cost = Decimal(paid_units) * package_price
        applied = _mk(f"{n}x{m}")
    elif ptype == "second_unit" and rule.percentage_discount is not None:
        n = rule.required_quantity or 2
        pct = rule.percentage_discount
        discounted_units = packages // n
        full_units = packages - discounted_units
        cost = (
            Decimal(full_units) * package_price
            + Decimal(discounted_units) * package_price * (Decimal("1") - pct)
        )
        applied = _mk(f"2ª unidad -{(pct * 100).normalize()}%")
    elif ptype in {"percentage", "min_quantity"} and rule.percentage_discount is not None:
        if rule.required_quantity is None or packages >= rule.required_quantity:
            pct = rule.percentage_discount
            cost = base * (Decimal("1") - pct)
            applied = _mk(f"-{(pct * 100).normalize()}%")
    elif ptype == "fixed" and rule.fixed_discount is not None:
        cost = base - rule.fixed_discount
        if cost < 0:
            cost = Decimal("0")
        applied = _mk(f"-{rule.fixed_discount} descuento")

    return cost, applied


# --------------------------------------------------------------------------- #
# Resolution
# --------------------------------------------------------------------------- #
class BasketResolver:
    """Resolves a NutriPlan basket against a chain/store's current prices."""

    def __init__(self, price_service: CurrentPriceService | None = None) -> None:
        self._prices = price_service or CurrentPriceService()
        self._converter = UnitConverter()

    def resolve(
        self,
        db: Session,
        *,
        retailer_id: int,
        store_id: int | None,
        retailer_public_id: uuid.UUID,
        store_public_id: uuid.UUID | None,
        items: list[BasketItem],
        as_of: datetime,
        currency: str = "EUR",
    ) -> BasketResolution:
        resolution = BasketResolution(
            retailer_id=retailer_public_id,
            store_id=store_public_id,
            as_of=as_of,
            currency=currency,
        )
        for item in items:
            line, unresolved = self._resolve_item(
                db, item, retailer_id=retailer_id, store_id=store_id, as_of=as_of
            )
            if line is not None:
                resolution.lines.append(line)
            elif unresolved is not None:
                resolution.unresolved.append(unresolved)
        return resolution

    # -- internals ---------------------------------------------------------- #
    def _resolve_item(
        self,
        db: Session,
        item: BasketItem,
        *,
        retailer_id: int,
        store_id: int | None,
        as_of: datetime,
    ) -> tuple[ResolvedLine | None, UnresolvedLine | None]:
        candidates = self._candidate_variants(db, item, retailer_id=retailer_id)
        if not candidates:
            return None, UnresolvedLine(
                item=item, reason="no_match", detail="Ningún producto coincide"
            )

        chosen: tuple[ProductVariant, CurrentPrice] | None = None
        for variant in candidates:
            price = self._prices.current(
                db, variant.id, store_id=store_id, as_of=as_of
            )
            if price is None:
                continue
            if chosen is None or self._prefer(price, chosen[1]):
                chosen = (variant, price)

        if chosen is None:
            return None, UnresolvedLine(
                item=item,
                reason="no_price",
                detail="Producto encontrado sin precio disponible",
                matched_variant_id=candidates[0].public_id,
            )

        variant, price = chosen
        if variant.package_quantity is None or variant.package_unit is None:
            return None, UnresolvedLine(
                item=item,
                reason="no_package_info",
                detail="El producto no declara formato de envase",
                matched_variant_id=variant.public_id,
            )

        try:
            needed = self._converter.convert(
                item.required_quantity, item.unit, variant.package_unit
            )
        except ConversionError:
            return None, UnresolvedLine(
                item=item,
                reason="unit_incompatible",
                detail=(
                    f"No se puede convertir {item.unit} a {variant.package_unit}"
                ),
                matched_variant_id=variant.public_id,
            )

        result = compute_packages(
            needed, Decimal("0"), variant.package_quantity, price.amount
        )
        rule = self._promotion_rule(db, variant.id, price)
        line_cost, promotion = apply_promotion(rule, result.packages, price.amount)

        return (
            ResolvedLine(
                item=item,
                variant_id=variant.public_id,
                product_id=self._product_public_id(db, variant.product_id),
                display_name=variant.display_name,
                required_quantity=item.required_quantity,
                required_unit=item.unit,
                package_quantity=variant.package_quantity,
                package_unit=variant.package_unit,
                packages=result.packages,
                purchased_quantity=result.purchased,
                used_quantity=result.used,
                leftover=result.leftover,
                unit_price=price.amount,
                list_cost=result.total_cost,
                line_cost=line_cost,
                currency=price.currency,
                promotion=promotion,
                price_type=price.price_type,
                price_scope=price.price_scope,
                source_id=price.source_id,
                observed_at=price.observed_at,
                age_seconds=Decimal(str(price.age.total_seconds())),
                freshness=price.status,
                confidence=price.confidence,
                available=price.available,
            ),
            None,
        )

    def _candidate_variants(
        self, db: Session, item: BasketItem, *, retailer_id: int
    ) -> list[ProductVariant]:
        stmt = select(ProductVariant).where(
            ProductVariant.retailer_id == retailer_id,
            ProductVariant.active.is_(True),
        )
        if item.variant_id is not None:
            stmt = stmt.where(ProductVariant.public_id == item.variant_id)
        elif item.product_id is not None:
            product = db.execute(
                select(Product.id).where(Product.public_id == item.product_id)
            ).scalar_one_or_none()
            if product is None:
                return []
            stmt = stmt.where(ProductVariant.product_id == product)
        elif item.ingredient is not None:
            pattern = f"%{item.ingredient.strip()}%"
            stmt = stmt.join(
                Product, Product.id == ProductVariant.product_id, isouter=True
            ).where(
                ProductVariant.display_name.ilike(pattern)
                | Product.name.ilike(pattern)
            )
        else:
            return []
        return list(db.execute(stmt).scalars().all())

    @staticmethod
    def _prefer(candidate: CurrentPrice, current: CurrentPrice) -> bool:
        """Prefer a real (non-estimated) price, then the cheaper one."""
        cand_est = candidate.price_type == _ESTIMATED
        cur_est = current.price_type == _ESTIMATED
        if cand_est != cur_est:
            return not cand_est
        return candidate.amount < current.amount

    def _promotion_rule(
        self, db: Session, variant_id: int, price: CurrentPrice
    ) -> PromotionRule | None:
        """The structured promotion for the exact observation ``price`` came from."""
        obs_stmt = (
            select(PriceObservation.id)
            .where(
                PriceObservation.product_variant_id == variant_id,
                PriceObservation.price_scope == price.price_scope,
                PriceObservation.observed_at == price.observed_at,
            )
            .order_by(PriceObservation.id.desc())
            .limit(1)
        )
        if price.store_id is not None:
            obs_stmt = obs_stmt.where(PriceObservation.store_id == price.store_id)
        observation_id = db.execute(obs_stmt).scalar_one_or_none()
        if observation_id is None:
            return None
        return db.execute(
            select(PromotionRule)
            .where(PromotionRule.price_observation_id == observation_id)
            .order_by(PromotionRule.id.desc())
            .limit(1)
        ).scalars().first()

    @staticmethod
    def _product_public_id(db: Session, product_id: int | None) -> uuid.UUID | None:
        if product_id is None:
            return None
        return db.execute(
            select(Product.public_id).where(Product.id == product_id)
        ).scalar_one_or_none()


def resolve_basket(
    db: Session,
    *,
    retailer_id: int,
    store_id: int | None,
    retailer_public_id: uuid.UUID,
    store_public_id: uuid.UUID | None,
    items: list[BasketItem],
    as_of: datetime,
    currency: str = "EUR",
) -> BasketResolution:
    """Resolve ``items`` against a chain/store's current prices (see module docstring)."""
    return BasketResolver().resolve(
        db,
        retailer_id=retailer_id,
        store_id=store_id,
        retailer_public_id=retailer_public_id,
        store_public_id=store_public_id,
        items=items,
        as_of=as_of,
        currency=currency,
    )


__all__ = [
    "BasketItem",
    "BasketResolution",
    "BasketResolver",
    "PromotionApplied",
    "ResolvedLine",
    "UnresolvedLine",
    "apply_promotion",
    "resolve_basket",
]
