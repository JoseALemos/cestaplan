"""Exact, Decimal-only unit conversion (OPTIMIZATION.md §2.2).

Mass (g, kg) and volume (ml, l) convert within their own dimension by fixed
factors. Crossing dimensions (volume <-> mass) or converting counted units
requires an explicit per-ingredient :class:`IngredientConversionDTO` density —
there are no silent approximations. If a conversion is undefined we raise, we
never guess.
"""

from __future__ import annotations

from decimal import Decimal

from cestaplan_engine.contracts import IngredientConversionDTO


class ConversionError(ValueError):
    """Raised when a requested unit conversion is not defined."""


# Base-unit factors within a single dimension. value * factor == value_in_base.
_MASS_TO_G: dict[str, Decimal] = {
    "g": Decimal("1"),
    "kg": Decimal("1000"),
    "mg": Decimal("0.001"),
}
_VOLUME_TO_ML: dict[str, Decimal] = {
    "ml": Decimal("1"),
    "l": Decimal("1000"),
    "cl": Decimal("10"),
}
# Counted units are dimensionless-per-unit; they only convert to themselves.
_COUNT_UNITS = {"unit", "ud", "piece", "pcs"}


def _norm_unit(unit: str) -> str:
    return unit.strip().lower()


class UnitConverter:
    """Converts quantities between units, exactly, in :class:`Decimal`."""

    def __init__(self, conversions: list[IngredientConversionDTO] | None = None) -> None:
        # Key: (canonical_name, from_unit, to_unit) -> factor
        self._factors: dict[tuple[str, str, str], Decimal] = {}
        for conv in conversions or []:
            f = _norm_unit(conv.from_unit)
            t = _norm_unit(conv.to_unit)
            name = conv.canonical_name
            self._factors[(name, f, t)] = conv.factor
            if conv.factor != 0:
                self._factors[(name, t, f)] = Decimal("1") / conv.factor

    def convert(
        self,
        quantity: Decimal,
        from_unit: str,
        to_unit: str,
        canonical_name: str | None = None,
    ) -> Decimal:
        """Convert ``quantity`` from ``from_unit`` to ``to_unit`` exactly."""
        f = _norm_unit(from_unit)
        t = _norm_unit(to_unit)
        if f == t:
            return quantity

        # Within mass.
        if f in _MASS_TO_G and t in _MASS_TO_G:
            return quantity * _MASS_TO_G[f] / _MASS_TO_G[t]
        # Within volume.
        if f in _VOLUME_TO_ML and t in _VOLUME_TO_ML:
            return quantity * _VOLUME_TO_ML[f] / _VOLUME_TO_ML[t]
        # Counted units only convert to themselves (already handled by f == t).
        if f in _COUNT_UNITS or t in _COUNT_UNITS:
            raise ConversionError(
                f"Cannot convert counted unit {from_unit!r} <-> {to_unit!r} "
                f"for {canonical_name!r} without an explicit conversion."
            )

        # Cross-dimension (or unknown units): need a per-ingredient density.
        if canonical_name is not None:
            direct = self._factors.get((canonical_name, f, t))
            if direct is not None:
                return quantity * direct
            # Bridge through a shared base unit if the density lands in g or ml.
            bridged = self._bridge(quantity, f, t, canonical_name)
            if bridged is not None:
                return bridged

        raise ConversionError(
            f"No conversion defined from {from_unit!r} to {to_unit!r} "
            f"for ingredient {canonical_name!r}."
        )

    def _bridge(
        self, quantity: Decimal, f: str, t: str, canonical_name: str
    ) -> Decimal | None:
        """Combine a declared density with in-dimension factors (e.g. l -> g via ml->g)."""
        for (name, cf, ct), factor in self._factors.items():
            if name != canonical_name:
                continue
            # f -> cf (same dimension) -> ct -> t (same dimension)
            try:
                to_cf = self.convert(quantity, f, cf)
            except ConversionError:
                continue
            crossed = to_cf * factor
            try:
                return self.convert(crossed, ct, t)
            except ConversionError:
                continue
        return None

    def can_convert(
        self, from_unit: str, to_unit: str, canonical_name: str | None = None
    ) -> bool:
        """Whether :meth:`convert` would succeed (no exception)."""
        try:
            self.convert(Decimal("1"), from_unit, to_unit, canonical_name)
        except ConversionError:
            return False
        return True


class IngredientNormalizer:
    """Resolve a recipe ingredient's canonical name against known aliases.

    The engine does not invent ingredients: unknown names are passed through and
    flagged ``unresolved`` so callers can warn, never silently guessing a match.
    """

    def __init__(self, aliases: dict[str, str] | None = None) -> None:
        # alias (lowercased) -> canonical_name
        self._aliases = {k.strip().lower(): v for k, v in (aliases or {}).items()}

    def normalize(self, canonical_name: str, display_name: str = "") -> tuple[str, bool]:
        """Return ``(resolved_canonical_name, matched)``."""
        key = canonical_name.strip().lower()
        if key in self._aliases:
            return self._aliases[key], True
        alt = display_name.strip().lower()
        if alt and alt in self._aliases:
            return self._aliases[alt], True
        # No alias table entry: keep the proposed canonical name, mark unmatched.
        return canonical_name, canonical_name.strip() != ""
