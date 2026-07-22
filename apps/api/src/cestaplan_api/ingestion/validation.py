"""Validation of normalized price observations before persistence (spec §11).

Pure logic, no DB and no network. :class:`ObservationValidator` inspects a
:class:`~cestaplan_api.ingestion.contracts.NormalizedObservation` together with a
:class:`ValidationContext` (store-link presence, block-page signal, package used to
recompute the unit price, clock skew) and returns a
:class:`~cestaplan_api.ingestion.contracts.ValidationResult`.

Invariants enforced:
- amount strictly positive; currency known; package quantity/unit recognised.
- unit price coherent with amount + package (catches x100 mistakes).
- external id present; ``observed_at`` not absurdly in the future.
- promotion validity window sane (``valid_from <= valid_until``).
- ``price_scope`` declared; ``exact_store`` requires a real store link (§7).
- observations derived from a block/login/CAPTCHA/error page are rejected and routed
  to quarantine (never treated as a real price).

Values are never invented: a missing field is reported as an error, not defaulted.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from cestaplan_api.ingestion.contracts import (
    AnomalyType,
    NormalizedObservation,
    PriceScope,
    Severity,
    ValidationResult,
)
from cestaplan_api.ingestion.normalization import NormalizationError, PriceNormalizer

#: HTTP statuses that indicate the response was a block/login/error, not price data.
_BLOCK_STATUS = frozenset({401, 403, 407, 429, 451, 500, 502, 503})

_SEVERITY_RANK: dict[Severity, int] = {
    Severity.LOW: 0,
    Severity.MEDIUM: 1,
    Severity.HIGH: 2,
    Severity.CRITICAL: 3,
}


@dataclass(frozen=True, slots=True)
class FieldError:
    """A structured validation error: which field, what went wrong, how severe."""

    field: str
    message: str
    severity: Severity = Severity.HIGH
    anomaly_type: AnomalyType | None = None

    def render(self) -> str:
        return f"{self.field}: {self.message} [{self.severity.value}]"


@dataclass(frozen=True, slots=True)
class ValidationContext:
    """Everything the validator needs beyond the observation itself.

    ``has_store_link`` states whether the observation resolves to a concrete store —
    required before an ``exact_store`` scope may be claimed. ``is_block_page`` /
    ``status_code`` carry the fetch signal used to quarantine non-price responses.
    The ``package_*`` fields let the validator recompute the unit price and check
    coherence.
    """

    has_store_link: bool = False
    is_block_page: bool = False
    status_code: int | None = None
    now: datetime | None = None
    package_quantity: object | None = None
    package_unit: str | None = None
    package_count: int = 1
    known_currencies: frozenset[str] = frozenset({"EUR"})
    max_future_skew: timedelta = timedelta(hours=24)
    coherence_tolerance: Decimal = Decimal("0.02")


@dataclass(frozen=True, slots=True)
class ObservationValidationReport:
    """Rich, structured outcome kept alongside the contract :class:`ValidationResult`."""

    valid: bool
    field_errors: tuple[FieldError, ...] = ()
    warnings: tuple[str, ...] = ()

    def to_result(self) -> ValidationResult:
        worst = _worst(self.field_errors)
        return ValidationResult(
            valid=self.valid,
            supported=True,
            errors=tuple(e.render() for e in self.field_errors),
            warnings=self.warnings,
            anomaly_type=worst[0] if worst else None,
            severity=worst[1] if worst else None,
        )


def _worst(errors: tuple[FieldError, ...]) -> tuple[AnomalyType | None, Severity] | None:
    best: FieldError | None = None
    for e in errors:
        if best is None or _SEVERITY_RANK[e.severity] > _SEVERITY_RANK[best.severity]:
            best = e
    if best is None:
        return None
    return best.anomaly_type, best.severity


class ObservationValidator:
    """Validates a normalized observation against ingestion rules (spec §11 / §7)."""

    def __init__(self) -> None:
        self._price_normalizer = PriceNormalizer()

    def validate(
        self, obs: NormalizedObservation, context: ValidationContext | None = None
    ) -> ValidationResult:
        return self.validate_report(obs, context).to_result()

    def validate_report(
        self, obs: NormalizedObservation, context: ValidationContext | None = None
    ) -> ObservationValidationReport:
        ctx = context or ValidationContext()
        now = ctx.now or datetime.now(UTC)
        errors: list[FieldError] = []
        warnings: list[str] = []

        # A block/login/CAPTCHA/error response is never a price — quarantine it.
        if ctx.is_block_page or (ctx.status_code in _BLOCK_STATUS):
            errors.append(
                FieldError(
                    "source",
                    "response is a block/login/CAPTCHA/error page, not price data",
                    Severity.CRITICAL,
                    AnomalyType.BLOCK_PAGE,
                )
            )
            # A block page invalidates everything else; report immediately.
            return ObservationValidationReport(False, tuple(errors), tuple(warnings))

        # amount > 0
        if obs.amount is None:
            errors.append(
                FieldError("amount", "missing amount", Severity.CRITICAL,
                           AnomalyType.MISSING_FIELD)
            )
        elif obs.amount <= 0:
            errors.append(
                FieldError("amount", "amount must be > 0", Severity.CRITICAL,
                           AnomalyType.ZERO_OR_NEGATIVE)
            )

        # currency
        currency = (obs.currency or "").upper()
        if currency not in {c.upper() for c in ctx.known_currencies}:
            errors.append(
                FieldError("currency", f"unknown currency {obs.currency!r}",
                           Severity.HIGH, AnomalyType.CURRENCY_MISMATCH)
            )

        # external id / variant reference present and stable
        if not obs.variant_ref or not obs.variant_ref.strip():
            errors.append(
                FieldError("variant_ref", "missing external product reference",
                           Severity.HIGH, AnomalyType.MISSING_FIELD)
            )

        # package quantity / unit recognised, and unit price coherent
        self._check_package_and_unit_price(obs, ctx, errors, warnings)

        # observed_at not absurdly in the future
        if obs.observed_at is not None:
            observed = obs.observed_at
            if observed.tzinfo is None:
                observed = observed.replace(tzinfo=UTC)
            if observed > now + ctx.max_future_skew:
                errors.append(
                    FieldError("observed_at", "observed_at is in the future",
                               Severity.HIGH, AnomalyType.STALE)
                )

        # price scope declared; exact_store requires a store link (§7)
        if obs.price_scope is PriceScope.UNKNOWN:
            errors.append(
                FieldError("price_scope", "price scope not declared", Severity.HIGH,
                           AnomalyType.MISSING_FIELD)
            )
        elif obs.price_scope is PriceScope.EXACT_STORE and not ctx.has_store_link:
            errors.append(
                FieldError(
                    "price_scope",
                    "exact_store scope requires a resolved store link",
                    Severity.HIGH,
                    AnomalyType.MISSING_FIELD,
                )
            )

        # promotion validity window sane
        promo = obs.promotion
        if (
            promo is not None
            and promo.valid_from is not None
            and promo.valid_until is not None
            and promo.valid_until < promo.valid_from
        ):
            errors.append(
                FieldError("promotion", "valid_until is before valid_from",
                           Severity.MEDIUM)
            )

        return ObservationValidationReport(
            valid=len(errors) == 0, field_errors=tuple(errors), warnings=tuple(warnings)
        )

    def _check_package_and_unit_price(
        self,
        obs: NormalizedObservation,
        ctx: ValidationContext,
        errors: list[FieldError],
        warnings: list[str],
    ) -> None:
        if ctx.package_unit is None and ctx.package_quantity is None:
            if obs.unit_amount is None:
                warnings.append("unit_amount: not provided and no package to recompute it")
            return

        try:
            recomputed = self._price_normalizer.normalize(
                obs.amount,
                obs.currency,
                package_quantity=ctx.package_quantity,
                package_unit=ctx.package_unit,
                package_count=ctx.package_count,
            )
        except NormalizationError as exc:
            errors.append(
                FieldError("package", str(exc), Severity.HIGH, AnomalyType.UNIT_MISMATCH)
            )
            return

        expected = recomputed.unit_amount
        if expected is None:
            errors.append(
                FieldError("package", "invalid package quantity/unit", Severity.HIGH,
                           AnomalyType.MISSING_FIELD)
            )
            return

        if obs.unit_code is not None and recomputed.unit_code is not None and (
            obs.unit_code != recomputed.unit_code
        ):
            errors.append(
                FieldError(
                    "unit_code",
                    f"unit_code {obs.unit_code!r} does not match package "
                    f"{recomputed.unit_code!r}",
                    Severity.HIGH,
                    AnomalyType.UNIT_MISMATCH,
                )
            )

        if obs.unit_amount is None:
            warnings.append(f"unit_amount: missing; expected ~{expected}")
            return

        if expected > 0:
            rel = abs(obs.unit_amount - expected) / expected
            if rel > ctx.coherence_tolerance:
                errors.append(
                    FieldError(
                        "unit_amount",
                        f"unit price {obs.unit_amount} incoherent with amount/package "
                        f"(expected ~{expected})",
                        Severity.HIGH,
                        AnomalyType.UNIT_MISMATCH,
                    )
                )


__all__ = [
    "FieldError",
    "ObservationValidationReport",
    "ObservationValidator",
    "ValidationContext",
]
