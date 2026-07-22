"""Whole-package selection — the crown-jewel calculation (OPTIMIZATION.md §3).

CestaPlan buys WHOLE packages. It never computes ``needed / package_size * price``
(that yields an impossible fractional cost). It buys an integer number of
packages that covers the pending quantity and tracks the surplus. All arithmetic
is exact :class:`Decimal`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_CEILING, Decimal

from cestaplan_engine.contracts import PackageOptionDTO


def decimal_ceil_div(pending: Decimal, package_quantity: Decimal) -> int:
    """``ceil(pending / package_quantity)`` computed exactly, returning an int."""
    if package_quantity <= 0:
        raise ValueError("package_quantity must be positive")
    if pending <= 0:
        return 0
    return int((pending / package_quantity).to_integral_value(rounding=ROUND_CEILING))


@dataclass(frozen=True)
class PackageResult:
    """Full audit trail for provisioning one product from one package format."""

    needed: Decimal
    pantry: Decimal
    pending: Decimal
    package_quantity: Decimal
    package_price: Decimal
    packages: int
    purchased: Decimal
    used: Decimal
    leftover: Decimal
    total_cost: Decimal


def compute_packages(
    needed: Decimal,
    pantry_available: Decimal,
    package_quantity: Decimal,
    package_price: Decimal,
) -> PackageResult:
    """Canonical whole-package computation (OPTIMIZATION.md §3.2).

    Example: 600 g needed, 500 g packs, empty pantry -> 2 packs, 1000 g bought,
    600 g used, 400 g leftover, cost = 2 x price. Never 600/500 x price.
    """
    pending = needed - pantry_available
    if pending < 0:
        pending = Decimal("0")
    packages = decimal_ceil_div(pending, package_quantity)
    purchased = Decimal(packages) * package_quantity
    pantry_used = needed - pending  # what the pantry covered
    used = pending  # what is drawn from the purchased packages
    leftover = purchased - used
    total_cost = Decimal(packages) * package_price
    return PackageResult(
        needed=needed,
        pantry=pantry_used,
        pending=pending,
        package_quantity=package_quantity,
        package_price=package_price,
        packages=packages,
        purchased=purchased,
        used=used,
        leftover=leftover,
        total_cost=total_cost,
    )


@dataclass(frozen=True)
class PackageChoice:
    """A chosen package format plus its provisioning result and price validity."""

    option: PackageOptionDTO
    result: PackageResult
    price_known: bool
    expired: bool


def _is_expired(option: PackageOptionDTO, as_of: date | None) -> bool:
    return (
        option.expires_at is not None
        and as_of is not None
        and option.expires_at < as_of
    )


class PackageOptimizer:
    """Chooses whole packages, doing a discrete search when formats differ (§5.4)."""

    def choose(
        self,
        pending: Decimal,
        options: list[PackageOptionDTO],
        as_of: date | None = None,
        w_waste: Decimal = Decimal("1.0"),
        w_cost: Decimal = Decimal("1.5"),
    ) -> PackageChoice | None:
        """Pick the package format minimizing ``w_waste*leftover + w_cost*cost``.

        Prices with a real value are preferred over estimates; ties break by
        product_id + package_quantity so the result is deterministic.
        """
        if not options:
            return None

        best: PackageChoice | None = None
        best_key: tuple | None = None
        for opt in options:
            if opt.package_quantity <= 0:
                continue
            expired = _is_expired(opt, as_of)
            price_known = bool(opt.has_price) and not expired
            price = opt.amount
            result = compute_packages(pending, Decimal("0"), opt.package_quantity, price)
            score = w_waste * result.leftover + w_cost * result.total_cost
            # Prefer known prices (0) over estimated (1); then lower score; then stable id.
            key = (
                0 if price_known else 1,
                score,
                opt.product_id,
                opt.package_quantity,
            )
            if best_key is None or key < best_key:
                best_key = key
                best = PackageChoice(
                    option=opt,
                    result=result,
                    price_known=price_known,
                    expired=expired,
                )
        return best
