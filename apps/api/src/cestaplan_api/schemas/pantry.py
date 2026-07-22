"""Pantry (despensa) request/response schemas.

A pantry item is household stock that reduces what a plan must buy: the deterministic
engine's ``PantryCalculator`` subtracts it from the shopping list (see
``services.planning_context``). Quantities are ``Decimal`` and serialised as strings,
never floats, consistent with the project's no-float rule. Units are validated against
the engine's known mass/volume/count units so an item can actually be reconciled.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from pydantic import BaseModel, Field, field_validator

# Units the deterministic engine can reconcile (cestaplan_engine.units): mass, volume and
# counted units. Anything else cannot be subtracted from a recipe requirement, so we reject
# it at the edge rather than storing an unusable row.
KNOWN_UNITS: frozenset[str] = frozenset(
    {"g", "kg", "mg", "ml", "l", "cl", "unit", "ud", "piece", "pcs"}
)

_MAX_QUANTITY = Decimal("1000000")


def _normalise_unit(value: str) -> str:
    unit = value.strip().lower()
    if unit not in KNOWN_UNITS:
        raise ValueError(
            f"Unidad no reconocida: {value!r}. "
            f"Usa una de: {', '.join(sorted(KNOWN_UNITS))}."
        )
    return unit


class PantryItemCreate(BaseModel):
    """Add stock to the pantry.

    ``name`` is a canonical ingredient name or free text; it is resolved to a known
    :class:`~cestaplan_api.models.Ingredient` on the server (case-insensitive on the
    canonical or display name). An item that cannot be resolved is rejected, because an
    unmapped item would never reduce a shopping list.
    """

    name: str = Field(min_length=1, max_length=200)
    quantity: Decimal = Field(gt=0, le=_MAX_QUANTITY)
    unit: str = Field(min_length=1, max_length=20)
    expires_at: date | None = None

    @field_validator("unit")
    @classmethod
    def _check_unit(cls, value: str) -> str:
        return _normalise_unit(value)


class PantryItemUpdate(BaseModel):
    """Partial update. Only provided fields change; send ``expires_at: null`` to clear it."""

    quantity: Decimal | None = Field(default=None, gt=0, le=_MAX_QUANTITY)
    unit: str | None = Field(default=None, min_length=1, max_length=20)
    expires_at: date | None = None

    @field_validator("unit")
    @classmethod
    def _check_unit(cls, value: str | None) -> str | None:
        return _normalise_unit(value) if value is not None else None


class PantryItemResponse(BaseModel):
    id: uuid.UUID
    canonical_name: str
    display: str
    quantity: str
    unit: str
    expires_at: date | None

    @classmethod
    def from_model(cls, item, ingredient) -> PantryItemResponse:
        return cls(
            id=item.public_id,
            canonical_name=ingredient.canonical_name,
            display=ingredient.display_name,
            quantity=str(item.quantity),
            unit=item.unit,
            expires_at=item.expires_at.date() if item.expires_at else None,
        )
