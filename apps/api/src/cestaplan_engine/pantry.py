"""Pantry accounting (OPTIMIZATION.md §2.5).

Discounts what is already at home from what a recipe needs, giving the pending
quantity to buy. Expired pantry stock is ignored. The pantry lowers cost but
never changes the quantity a recipe actually uses.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from cestaplan_engine.contracts import PantryItemDTO
from cestaplan_engine.units import ConversionError, UnitConverter


class PantryCalculator:
    """Tracks available (non-expired) pantry quantities per canonical ingredient."""

    def __init__(
        self,
        pantry: list[PantryItemDTO],
        converter: UnitConverter,
        as_of: date | None = None,
    ) -> None:
        self._converter = converter
        self._as_of = as_of
        # canonical_name -> list of (quantity, unit) still valid.
        self._items: dict[str, list[PantryItemDTO]] = {}
        for item in pantry:
            if item.expires_at is not None and as_of is not None and item.expires_at < as_of:
                continue  # expired: not usable.
            self._items.setdefault(item.canonical_name, []).append(item)

    def available(self, canonical_name: str, unit: str) -> Decimal:
        """Non-expired pantry quantity for ``canonical_name`` expressed in ``unit``."""
        total = Decimal("0")
        for item in self._items.get(canonical_name, []):
            try:
                total += self._converter.convert(
                    item.quantity, item.unit, unit, canonical_name
                )
            except ConversionError:
                # Incompatible unit -> cannot safely subtract; skip (conservative).
                continue
        return total

    def pending(
        self, canonical_name: str, needed: Decimal, unit: str
    ) -> tuple[Decimal, Decimal]:
        """Return ``(pantry_used, pending)`` for a needed quantity in ``unit``.

        ``pending = max(0, needed - pantry_used)`` (formula §2.5).
        """
        avail = self.available(canonical_name, unit)
        used = min(needed, avail)
        pending = needed - used
        if pending < 0:
            pending = Decimal("0")
        return used, pending
