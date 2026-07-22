"""Normalization of parsed products and prices for the ingestion pipeline.

Pure logic, no DB and no network: takes plain parsed inputs and returns immutable
value objects the caller persists. Money and physical quantities are always
:class:`decimal.Decimal` — never ``float`` — and a *missing* value is ``None``,
never ``0`` (see PRICE_SOURCES_GUIDE: "No sustituir ausente por 0").

Unit conventions mirror :mod:`cestaplan_engine.units`: mass canonicalises to
``g``/``kg``, volume to ``ml``/``l`` and counted goods to ``unit``. A per-measure
unit price is expressed against the canonical base of its dimension: ``€/kg`` for
mass, ``€/l`` for volume and ``€/unit`` for counted goods.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from cestaplan_api.ingestion.contracts import PromotionInfo, PromotionType

# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


class NormalizationError(ValueError):
    """Raised when an input cannot be normalized (unknown currency/unit, bad number)."""


# --------------------------------------------------------------------------- #
# Unit conventions (kept consistent with cestaplan_engine.units)
# --------------------------------------------------------------------------- #

#: Money is quantized to whole cents; per-measure unit prices keep more precision.
_MONEY_Q = Decimal("0.01")
_UNIT_PRICE_Q = Decimal("0.0001")

# Synonym -> canonical package unit (one of g/kg/ml/l/unit).
_UNIT_SYNONYMS: dict[str, str] = {
    "g": "g", "gr": "g", "grs": "g", "gram": "g", "grams": "g",
    "gramo": "g", "gramos": "g",
    "kg": "kg", "kgs": "kg", "kilo": "kg", "kilos": "kg",
    "kilogramo": "kg", "kilogramos": "kg",
    "mg": "mg", "miligramo": "mg", "miligramos": "mg",
    "ml": "ml", "mililitro": "ml", "mililitros": "ml", "cc": "ml",
    "cl": "cl", "centilitro": "cl", "centilitros": "cl",
    "l": "l", "lt": "l", "lts": "l", "litro": "l", "litros": "l",
    "unit": "unit", "units": "unit", "u": "unit", "ud": "unit", "uds": "unit",
    "unidad": "unit", "unidades": "unit", "piece": "unit", "pieces": "unit",
    "pcs": "unit", "pack": "unit",
}

# Canonical unit -> (dimension base unit, factor to base). value * factor = value_in_base.
_TO_BASE: dict[str, tuple[str, Decimal]] = {
    "mg": ("kg", Decimal("0.000001")),
    "g": ("kg", Decimal("0.001")),
    "kg": ("kg", Decimal("1")),
    "ml": ("l", Decimal("0.001")),
    "cl": ("l", Decimal("0.01")),
    "l": ("l", Decimal("1")),
    "unit": ("unit", Decimal("1")),
}

# Canonical package unit reported on the normalized product (mg/cl fold into g/ml).
_CANONICAL_PACKAGE_UNIT: dict[str, str] = {
    "mg": "g", "g": "g", "kg": "kg",
    "cl": "ml", "ml": "ml", "l": "l",
    "unit": "unit",
}

_MULTIPACK_RE = re.compile(
    r"(\d+)\s*[x×]\s*(\d+(?:[.,]\d+)?)\s*(kg|g|gr|ml|cl|l|lt|unidades?|uds?|u)\b",  # noqa: RUF001
    re.IGNORECASE,
)
_WHITESPACE_RE = re.compile(r"\s+")


def canonical_unit(unit: str | None) -> str | None:
    """Map a raw unit token to its canonical form, or ``None`` if unrecognised."""
    if unit is None:
        return None
    key = _WHITESPACE_RE.sub("", unit.strip().lower())
    return _UNIT_SYNONYMS.get(key)


def to_decimal(value: object) -> Decimal | None:
    """Coerce ``value`` to :class:`Decimal` via ``str`` (never through ``float``).

    ``None`` and empty strings yield ``None`` (missing, not zero).
    """
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    text = str(value).strip().replace(",", ".")
    if not text:
        return None
    try:
        return Decimal(text)
    except InvalidOperation as exc:  # pragma: no cover - defensive
        raise NormalizationError(f"not a decimal: {value!r}") from exc


# --------------------------------------------------------------------------- #
# Product normalization
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ParsedProduct:
    """Raw parsed product fields handed to :class:`ProductNormalizer`."""

    name: str
    brand: str | None = None
    package_quantity: object | None = None
    package_unit: str | None = None
    package_count: int | None = None


@dataclass(frozen=True, slots=True)
class NormalizedProduct:
    """A cleaned product with canonical units and a base-unit total quantity."""

    name: str
    brand: str | None
    package_quantity: Decimal | None
    package_unit: str | None
    package_count: int
    base_quantity: Decimal | None
    base_unit: str | None


class ProductNormalizer:
    """Normalizes a parsed product: name cleanup, canonical units, multipack totals."""

    def normalize(self, parsed: ParsedProduct) -> NormalizedProduct:
        name = _WHITESPACE_RE.sub(" ", parsed.name or "").strip()
        brand = parsed.brand.strip() if parsed.brand and parsed.brand.strip() else None

        quantity = to_decimal(parsed.package_quantity)
        raw_unit = parsed.package_unit
        count = parsed.package_count

        # Recover multipack shape (e.g. "6 x 330 ml") from the name when not supplied.
        if quantity is None or raw_unit is None or count is None:
            m = _MULTIPACK_RE.search(parsed.name or "")
            if m is not None:
                if count is None:
                    count = int(m.group(1))
                if quantity is None:
                    quantity = to_decimal(m.group(2))
                if raw_unit is None:
                    raw_unit = m.group(3)

        package_count = count if count and count > 0 else 1

        canon = canonical_unit(raw_unit)
        if raw_unit is not None and canon is None:
            raise NormalizationError(f"unrecognised package unit: {raw_unit!r}")

        package_unit = _CANONICAL_PACKAGE_UNIT.get(canon) if canon else None
        base_quantity: Decimal | None = None
        base_unit: str | None = None
        if canon is not None and quantity is not None:
            base_unit, factor = _TO_BASE[canon]
            base_quantity = quantity * factor * Decimal(package_count)

        return NormalizedProduct(
            name=name,
            brand=brand,
            package_quantity=quantity,
            package_unit=package_unit,
            package_count=package_count,
            base_quantity=base_quantity,
            base_unit=base_unit,
        )


# --------------------------------------------------------------------------- #
# Price normalization
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class NormalizedPrice:
    """A normalized money amount plus a coherent per-measure unit price."""

    amount: Decimal | None
    currency: str
    unit_amount: Decimal | None
    unit_code: str | None


class PriceNormalizer:
    """Normalizes money amounts and derives ``€/kg`` · ``€/l`` · ``€/unit`` prices."""

    def __init__(self, allowed_currencies: frozenset[str] = frozenset({"EUR"})) -> None:
        self._allowed = frozenset(c.upper() for c in allowed_currencies)

    def normalize(
        self,
        amount: object | None,
        currency: str | None = "EUR",
        *,
        package_quantity: object | None = None,
        package_unit: str | None = None,
        package_count: int | None = 1,
    ) -> NormalizedPrice:
        """Return a :class:`NormalizedPrice`.

        A missing ``amount`` yields ``amount=None`` and ``unit_amount=None`` (never 0).
        An unknown currency is rejected. The unit price is computed coherently from the
        amount and the package's canonical base quantity.
        """
        code = (currency or "EUR").strip().upper()
        if code not in self._allowed:
            raise NormalizationError(f"unknown currency: {currency!r}")

        money = to_decimal(amount)
        if money is not None:
            money = money.quantize(_MONEY_Q, rounding=ROUND_HALF_UP)

        unit_amount, unit_code = self._unit_price(
            money, package_quantity, package_unit, package_count
        )
        return NormalizedPrice(
            amount=money, currency=code, unit_amount=unit_amount, unit_code=unit_code
        )

    def _unit_price(
        self,
        money: Decimal | None,
        package_quantity: object | None,
        package_unit: str | None,
        package_count: int | None,
    ) -> tuple[Decimal | None, str | None]:
        canon = canonical_unit(package_unit)
        if package_unit is not None and canon is None:
            raise NormalizationError(f"unrecognised package unit: {package_unit!r}")
        quantity = to_decimal(package_quantity)
        count = package_count if package_count and package_count > 0 else 1
        if canon is None or quantity is None or quantity <= 0:
            return None, canon and _TO_BASE[canon][0]
        base_unit, factor = _TO_BASE[canon]
        total_base = quantity * factor * Decimal(count)
        if money is None or total_base <= 0:
            return None, base_unit
        unit_amount = (money / total_base).quantize(_UNIT_PRICE_Q, rounding=ROUND_HALF_UP)
        return unit_amount, base_unit


# --------------------------------------------------------------------------- #
# Promotion parsing
# --------------------------------------------------------------------------- #

_NXM_RE = re.compile(
    r"(\d+)\s*[x×]\s*(\d+)\b(?!\s*(?:g|gr|kg|ml|cl|l|lt|unidad))", re.IGNORECASE  # noqa: RUF001
)
_NXM_WORDS_RE = re.compile(
    r"(?:compra|lleva|llevate|llévate)?\s*(\d+)\s*(?:por|paga(?:s)?)\s*(\d+)", re.IGNORECASE
)
_SECOND_UNIT_RE = re.compile(
    r"(?:2[ªaº]|segunda|second)\s*(?:unidad|unit)\s*(?:al\s*|a\s*|-|de\s*)?\s*(\d+(?:[.,]\d+)?)\s*%",
    re.IGNORECASE,
)
_SECOND_UNIT_FREE_RE = re.compile(
    r"(?:2[ªaº]|segunda|second)\s*(?:unidad|unit)\s*(?:gratis|free)", re.IGNORECASE
)
_PERCENT_RE = re.compile(
    r"(?:-|menos\s+)?\s*(\d+(?:[.,]\d+)?)\s*%\s*(?:de\s+)?"
    r"(?:dto\.?|descuento|off|discount|rebaja)?",
    re.IGNORECASE,
)
_FIXED_RE = re.compile(
    r"(?:-|ahorra|save|descuento\s+de)\s*(\d+(?:[.,]\d+)?)\s*(?:€|eur\b|euros?)",
    re.IGNORECASE,
)
_MIN_QTY_RE = re.compile(
    r"(?:min\.?|m[ií]nimo|a\s+partir\s+de|comprando)\s*(\d+)", re.IGNORECASE
)
_PACK_RE = re.compile(r"pack\s*(?:de\s*)?(\d+)", re.IGNORECASE)
_LOYALTY_RE = re.compile(
    r"tarjeta|socio|loyalty|club|fidelidad|con\s+tarjeta", re.IGNORECASE
)
_DATE_RANGE_RE = re.compile(
    r"del?\s+(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?\s+al?\s+"
    r"(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?",
    re.IGNORECASE,
)


def _pct(text: str) -> Decimal:
    return to_decimal(text) or Decimal("0")


def _parse_date(day: str, month: str, year: str | None, default_year: int) -> datetime:
    y = int(year) if year else default_year
    if y < 100:
        y += 2000
    return datetime(y, int(month), int(day), tzinfo=UTC)


class PromotionParser:
    """Parses free-text promotion labels into structured :class:`PromotionInfo` rules.

    The rule is modelled (required/charged quantities, discount amounts, validity
    window) so a cost engine can evaluate it per number of packages bought — it is
    never collapsed into a single effective unit price.
    """

    def parse(self, raw_text: str | None, *, now: datetime | None = None) -> PromotionInfo | None:
        if raw_text is None:
            return None
        text = raw_text.strip()
        if not text:
            return None

        now = now or datetime.now(UTC)
        loyalty = _LOYALTY_RE.search(text) is not None
        valid_from, valid_until = self._parse_dates(text, now.year)

        common: dict[str, object] = {
            "raw_text": raw_text,
            "loyalty_required": loyalty,
            "valid_from": valid_from,
            "valid_until": valid_until,
        }

        # Order matters: more specific shapes win over the bare percentage/fixed forms.
        m = _NXM_RE.search(text) or _NXM_WORDS_RE.search(text)
        if m is not None:
            required, charged = int(m.group(1)), int(m.group(2))
            if required > charged >= 0:
                return PromotionInfo(
                    promotion_type=PromotionType.NXM,
                    required_quantity=required,
                    charged_quantity=charged,
                    **common,  # type: ignore[arg-type]
                )

        if _SECOND_UNIT_FREE_RE.search(text) is not None:
            return PromotionInfo(
                promotion_type=PromotionType.SECOND_UNIT,
                required_quantity=2,
                percentage_discount=Decimal("100"),
                **common,  # type: ignore[arg-type]
            )
        m = _SECOND_UNIT_RE.search(text)
        if m is not None:
            return PromotionInfo(
                promotion_type=PromotionType.SECOND_UNIT,
                required_quantity=2,
                percentage_discount=_pct(m.group(1)),
                **common,  # type: ignore[arg-type]
            )

        m = _PACK_RE.search(text)
        if m is not None:
            return PromotionInfo(
                promotion_type=PromotionType.PACK,
                required_quantity=int(m.group(1)),
                **common,  # type: ignore[arg-type]
            )

        min_m = _MIN_QTY_RE.search(text)
        pct_m = _PERCENT_RE.search(text)
        fix_m = _FIXED_RE.search(text)
        if min_m is not None and (pct_m is not None or fix_m is not None):
            return PromotionInfo(
                promotion_type=PromotionType.MIN_QUANTITY,
                required_quantity=int(min_m.group(1)),
                percentage_discount=_pct(pct_m.group(1)) if pct_m else None,
                fixed_discount=to_decimal(fix_m.group(1)) if fix_m else None,
                **common,  # type: ignore[arg-type]
            )

        if fix_m is not None:
            return PromotionInfo(
                promotion_type=PromotionType.FIXED,
                fixed_discount=to_decimal(fix_m.group(1)),
                **common,  # type: ignore[arg-type]
            )
        if pct_m is not None:
            return PromotionInfo(
                promotion_type=PromotionType.PERCENTAGE,
                percentage_discount=_pct(pct_m.group(1)),
                **common,  # type: ignore[arg-type]
            )

        return None

    def _parse_dates(
        self, text: str, default_year: int
    ) -> tuple[datetime | None, datetime | None]:
        m = _DATE_RANGE_RE.search(text)
        if m is None:
            return None, None
        try:
            start = _parse_date(m.group(1), m.group(2), m.group(3), default_year)
            end = _parse_date(m.group(4), m.group(5), m.group(6), default_year)
        except (ValueError, TypeError):
            return None, None
        return start, end


__all__ = [
    "NormalizationError",
    "NormalizedPrice",
    "NormalizedProduct",
    "ParsedProduct",
    "PriceNormalizer",
    "ProductNormalizer",
    "PromotionParser",
    "canonical_unit",
    "to_decimal",
]
