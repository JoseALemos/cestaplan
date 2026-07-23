"""Production-activation gate + kill switch for price providers (spec §O/§S).

A provider does NOT reach production just because its API works. :func:`evaluate_production`
checks, per provider, that transport is operational, its mapper is verified, data quality is
accepted, its data rights are compatible with the use, and a human approved it — and that the
global kill switch is off and providers are enabled. This is independent of the technical
transport tests; it is the deliberate human gate.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from cestaplan_api.config import Settings
from cestaplan_api.ingestion.providers.exceptions import ProviderNotActivated
from cestaplan_api.models import ProviderActivation

# Data-rights statuses under which a production sync (fetch + store) is permitted.
_RIGHTS_OK_FOR_PROD = frozenset(
    {"commercial_use_allowed", "storage_allowed", "display_allowed"}
)


@dataclass(slots=True)
class ActivationDecision:
    allowed: bool
    reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {"allowed": self.allowed, "reasons": list(self.reasons)}


def get_activation(db: Session, provider_code: str) -> ProviderActivation | None:
    return db.execute(
        select(ProviderActivation).where(
            ProviderActivation.provider_code == provider_code
        )
    ).scalar_one_or_none()


def evaluate_production(
    db: Session, provider_code: str, settings: Settings
) -> ActivationDecision:
    """Return whether ``provider_code`` may run a PRODUCTION sync, with blocking reasons."""
    reasons: list[str] = []
    if settings.price_provider_kill_switch:
        reasons.append("kill_switch_on")
    if not settings.price_providers_enabled:
        reasons.append("price_providers_disabled")

    row = get_activation(db, provider_code)
    if row is None:
        reasons.append("no_activation_record")
        return ActivationDecision(False, reasons)

    if row.transport_status != "operational":
        reasons.append(f"transport_status={row.transport_status}")
    if row.mapper_status != "verified":
        reasons.append(f"mapper_status={row.mapper_status}")
    if row.data_quality_status != "accepted":
        reasons.append(f"data_quality_status={row.data_quality_status}")
    if (
        settings.provider_require_rights_approval
        and row.data_rights_status not in _RIGHTS_OK_FOR_PROD
    ):
        reasons.append(f"data_rights_status={row.data_rights_status}")
    if row.production_approved_at is None or row.production_approved_by is None:
        reasons.append("not_manually_approved")

    return ActivationDecision(not reasons, reasons)


def guard_production_sync(db: Session, provider_code: str, settings: Settings) -> None:
    """Raise :class:`ProviderNotActivated` unless a production sync is permitted."""
    decision = evaluate_production(db, provider_code, settings)
    if not decision.allowed:
        raise ProviderNotActivated(
            f"{provider_code} not cleared for production: {', '.join(decision.reasons)}"
        )


def can_run_development(db: Session, provider_code: str, settings: Settings) -> bool:
    """Dev syncs are allowed when the kill switch is off and the provider is dev-flagged.

    Production-cleared providers may also run in development.
    """
    if settings.price_provider_kill_switch:
        return False
    row = get_activation(db, provider_code)
    if row is None:
        return False
    return row.development_only or evaluate_production(db, provider_code, settings).allowed


__all__ = [
    "ActivationDecision",
    "can_run_development",
    "evaluate_production",
    "get_activation",
    "guard_production_sync",
]
