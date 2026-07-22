"""Request schemas and response serializers for the PRICES API (FASE B, §19).

Requests are validated with pydantic; responses are assembled as plain dicts with every
money amount and physical quantity serialized to a **string** (money is Decimal in the
service layer, strings on the wire). :func:`serialize_basket` turns a
:class:`BasketResolution` into the resolve-basket response shape.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field, model_validator

from cestaplan_api.services.basket_resolver import (
    BasketItem,
    BasketResolution,
    PromotionApplied,
    ResolvedLine,
    UnresolvedLine,
)

_MAX_ITEMS = 200


def _s(value: Any) -> str | None:
    """Serialize a value to a string. Decimals are rendered as minimal fixed-point
    (no trailing zeros, no exponent) so money and quantities are formatted consistently
    regardless of the ``Numeric`` scale they were read from."""
    if value is None:
        return None
    if isinstance(value, Decimal):
        text = format(value, "f")
        if "." in text:
            text = text.rstrip("0").rstrip(".")
        return text or "0"
    return str(value)


# --------------------------------------------------------------------------- #
# Request models
# --------------------------------------------------------------------------- #
class BasketItemIn(BaseModel):
    """One requested basket line. Exactly one of variant/product/ingredient identifies it."""

    variant_id: uuid.UUID | None = None
    product_id: uuid.UUID | None = None
    ingredient: str | None = Field(default=None, min_length=1, max_length=200)
    required_quantity: Decimal = Field(gt=0, le=1_000_000)
    unit: str = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def _one_identifier(self) -> BasketItemIn:
        provided = [
            v
            for v in (self.variant_id, self.product_id, self.ingredient)
            if v is not None
        ]
        if len(provided) != 1:
            raise ValueError(
                "cada artículo requiere exactamente uno de: "
                "variant_id, product_id, ingredient"
            )
        return self

    def to_item(self) -> BasketItem:
        return BasketItem(
            required_quantity=self.required_quantity,
            unit=self.unit,
            variant_id=self.variant_id,
            product_id=self.product_id,
            ingredient=self.ingredient,
        )


class ResolveBasketRequest(BaseModel):
    """Input to ``POST /prices/resolve-basket``: a store/chain, a date and the items."""

    store_id: uuid.UUID | None = None
    retailer_id: uuid.UUID | None = None
    target_date: date | None = None
    currency: str = Field(default="EUR", min_length=3, max_length=3)
    items: list[BasketItemIn] = Field(min_length=1, max_length=_MAX_ITEMS)

    @model_validator(mode="after")
    def _need_scope(self) -> ResolveBasketRequest:
        if self.store_id is None and self.retailer_id is None:
            raise ValueError("se requiere store_id o retailer_id")
        return self


# --------------------------------------------------------------------------- #
# Response serializers
# --------------------------------------------------------------------------- #
def _serialize_item(item: BasketItem) -> dict[str, Any]:
    return {
        "variant_id": _s(item.variant_id),
        "product_id": _s(item.product_id),
        "ingredient": item.ingredient,
        "required_quantity": _s(item.required_quantity),
        "unit": item.unit,
    }


def _serialize_promotion(promo: PromotionApplied | None) -> dict[str, Any] | None:
    if promo is None:
        return None
    return {
        "type": promo.type,
        "description": promo.description,
        "required_quantity": promo.required_quantity,
        "charged_quantity": promo.charged_quantity,
        "percentage_discount": _s(promo.percentage_discount),
        "fixed_discount": _s(promo.fixed_discount),
    }


def serialize_line(line: ResolvedLine) -> dict[str, Any]:
    return {
        "request": _serialize_item(line.item),
        "variant_id": str(line.variant_id),
        "product_id": _s(line.product_id),
        "display_name": line.display_name,
        "required_quantity": _s(line.required_quantity),
        "required_unit": line.required_unit,
        "package_quantity": _s(line.package_quantity),
        "package_unit": line.package_unit,
        "packages": line.packages,
        "purchased_quantity": _s(line.purchased_quantity),
        "used_quantity": _s(line.used_quantity),
        "leftover": _s(line.leftover),
        "unit_price": _s(line.unit_price),
        "list_cost": _s(line.list_cost),
        "line_cost": _s(line.line_cost),
        "currency": line.currency,
        "promotion_applied": line.promotion is not None,
        "promotion": _serialize_promotion(line.promotion),
        "price_type": line.price_type,
        "price_scope": line.price_scope,
        "source_id": line.source_id,
        "observed_at": line.observed_at.isoformat(),
        "age_seconds": _s(line.age_seconds),
        "freshness": line.freshness.value,
        "confidence": _s(line.confidence),
        "available": line.available,
        "is_estimated": line.is_estimated,
    }


def _serialize_unresolved(item: UnresolvedLine) -> dict[str, Any]:
    return {
        "request": _serialize_item(item.item),
        "reason": item.reason,
        "detail": item.detail,
        "matched_variant_id": _s(item.matched_variant_id),
    }


def serialize_basket(resolution: BasketResolution) -> dict[str, Any]:
    """Serialize a :class:`BasketResolution` into the resolve-basket response dict."""
    return {
        "retailer_id": str(resolution.retailer_id),
        "store_id": _s(resolution.store_id),
        "as_of": resolution.as_of.isoformat(),
        "currency": resolution.currency,
        "lines": [serialize_line(line) for line in resolution.lines],
        "unresolved": [_serialize_unresolved(item) for item in resolution.unresolved],
        "totals": {
            "known_cost": _s(resolution.known_cost),
            "estimated_cost": _s(resolution.estimated_cost),
            "total_cost": _s(resolution.total_cost),
            "currency": resolution.currency,
        },
        "coverage": {
            "resolved_count": len(resolution.lines),
            "unresolved_count": len(resolution.unresolved),
            "item_count": resolution.item_count,
            "coverage_ratio": _s(resolution.coverage_ratio),
        },
    }


__all__ = [
    "BasketItemIn",
    "ResolveBasketRequest",
    "serialize_basket",
    "serialize_line",
]
