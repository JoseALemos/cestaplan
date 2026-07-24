"""Idempotent bootstrap of price-source RIGHTS metadata into ``provider_activation``.

Records the *authorized* state of each source (the seven externally-owned chains authorized via
private commercial agreements, Open Prices under ODbL, and our own synthetic demo) from the
canonical :mod:`cestaplan_api.ingestion.providers.rights` registry. It writes ONLY rights /
authorization / licence-display / rights-scope fields.

Guarantees (spec §10/§11):
  * runs on a freshly-migrated DB (creates a minimal, production-OFF row when none exists);
  * seeds NO products and NO prices; makes NO external calls;
  * never enables production, costing, staging, shadow, mapper or quality — legal rights and
    production activation stay strictly separate;
  * idempotent: re-running makes no further changes;
  * never overwrites later administrative edits — it only FILLS values that are still unset,
    ``unknown`` or ``under_review`` (a value the operator changed on purpose is left untouched);
  * never writes secrets: the internal evidence/notes columns are never set here and their
    values are never printed.

Usage::

    python -m cestaplan_api.tools.bootstrap_source_rights --dry-run [--all | --provider CODE ...]
    python -m cestaplan_api.tools.bootstrap_source_rights --apply   [--all | --provider CODE ...]

In production, run ``--dry-run`` first and review the sanitized diff before ``--apply``.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from cestaplan_api.db import SessionLocal
from cestaplan_api.ingestion.providers.rights import SOURCE_RIGHTS, SourceRights
from cestaplan_api.models import ProviderActivation

# ``data_rights_status`` may be filled by the bootstrap only from these "not yet decided" values.
_RIGHTS_STATUS_FILLABLE = {None, "unknown", "under_review"}
# ``authorization_status`` may be filled only from its default (never downgrade a decision).
_AUTH_STATUS_FILLABLE = {None, "unknown"}
# Nullable display/scope fields: filled only when still unset (None).
_NULLABLE_RIGHTS_FIELDS = (
    "license_basis",
    "license_display_name",
    "rights_display_name",
    "rights_scope",
    "attribution_text_public",
)
# INTERNAL-ONLY columns: never written by the bootstrap, never printed.
_INTERNAL_FIELDS = ("internal_evidence_reference", "legal_notes_internal")


@dataclass(frozen=True)
class FieldChange:
    field: str
    old: object
    new: object


@dataclass
class SourcePlan:
    provider_code: str
    created: bool
    changes: list[FieldChange] = field(default_factory=list)

    @property
    def has_changes(self) -> bool:
        return self.created or bool(self.changes)


def _plan_source(row: ProviderActivation | None, rights: SourceRights, now: datetime) -> SourcePlan:
    """Compute the (fill-only) rights changes for one source without mutating anything."""
    created = row is None
    plan = SourcePlan(provider_code=rights.provider_code, created=created)

    def current(name: str) -> object:
        return None if created else getattr(row, name)

    # data_rights_status — fill only when still undecided.
    status = current("data_rights_status")
    if status in _RIGHTS_STATUS_FILLABLE and status != rights.data_rights_status:
        plan.changes.append(FieldChange("data_rights_status", status, rights.data_rights_status))

    # authorization_status (+ verified_at stamp) — fill only from the default.
    auth = current("authorization_status")
    if auth in _AUTH_STATUS_FILLABLE and auth != rights.authorization_status:
        plan.changes.append(
            FieldChange("authorization_status", auth, rights.authorization_status)
        )
        verified_now = rights.authorization_status == "verified"
        if verified_now and current("authorization_verified_at") is None:
            plan.changes.append(FieldChange("authorization_verified_at", None, now))

    # Nullable display/scope fields — fill only when unset.
    for name in _NULLABLE_RIGHTS_FIELDS:
        if current(name) is None:
            new_value = getattr(rights, name)
            if new_value is not None:
                plan.changes.append(FieldChange(name, None, new_value))

    return plan


def _apply_plan(
    db: Session, row: ProviderActivation | None, plan: SourcePlan
) -> ProviderActivation:
    """Apply a plan's fill changes. Creates a minimal, production-OFF row when none exists."""
    if row is None:
        row = ProviderActivation(provider_code=plan.provider_code)
        db.add(row)
    for change in plan.changes:
        setattr(row, change.field, change.new)
    db.flush()
    return row


def _target_codes(args: argparse.Namespace) -> list[str]:
    if args.all or not args.provider:
        return list(SOURCE_RIGHTS.keys())
    unknown = [c for c in args.provider if c not in SOURCE_RIGHTS]
    if unknown:
        raise SystemExit(f"unknown provider code(s): {', '.join(unknown)}")
    return list(args.provider)


def _sanitize(value: object) -> object:
    """Render a value for the diff. Internal columns are never planned, but redact defensively."""
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def plan_all(db: Session, codes: list[str], now: datetime) -> list[SourcePlan]:
    rows = {
        r.provider_code: r
        for r in db.execute(
            select(ProviderActivation).where(ProviderActivation.provider_code.in_(codes))
        ).scalars()
    }
    plans: list[SourcePlan] = []
    for code in codes:
        plans.append(_plan_source(rows.get(code), SOURCE_RIGHTS[code], now))
    return plans


def apply_to_session(db: Session, codes: list[str], now: datetime) -> list[SourcePlan]:
    """Plan and apply the fill changes within an existing session (does NOT commit).

    Shared by :func:`run` and the tests, so the exact fill-only semantics are exercised without a
    separate write path.
    """
    plans = plan_all(db, codes, now)
    rows = {
        r.provider_code: r
        for r in db.execute(
            select(ProviderActivation).where(ProviderActivation.provider_code.in_(codes))
        ).scalars()
    }
    for plan in plans:
        if plan.has_changes:
            _apply_plan(db, rows.get(plan.provider_code), plan)
    return plans


def run(*, apply: bool, codes: list[str], now: datetime | None = None) -> list[SourcePlan]:
    """Plan (and optionally apply) rights bootstrap for ``codes``. Returns the plans."""
    now = now or datetime.now(UTC)
    with SessionLocal() as db:
        if apply:
            plans = apply_to_session(db, codes, now)
            db.commit()
        else:
            plans = plan_all(db, codes, now)
    return plans


def _print_diff(plans: list[SourcePlan], *, applied: bool) -> None:
    payload = {
        "mode": "apply" if applied else "dry-run",
        "sources": [
            {
                "provider": p.provider_code,
                "created": p.created,
                "changes": [
                    {"field": c.field, "old": _sanitize(c.old), "new": _sanitize(c.new)}
                    for c in p.changes
                    if c.field not in _INTERNAL_FIELDS
                ],
            }
            for p in plans
        ],
        "sources_changed": sum(1 for p in plans if p.has_changes),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--dry-run", action="store_true", help="Show the sanitized diff; write nothing"
    )
    mode.add_argument("--apply", action="store_true", help="Apply the fill changes")
    parser.add_argument(
        "--provider", action="append", metavar="CODE", help="Limit to provider code(s); repeatable"
    )
    parser.add_argument("--all", action="store_true", help="All known sources (default)")
    args = parser.parse_args(argv)

    codes = _target_codes(args)
    plans = run(apply=bool(args.apply), codes=codes)
    _print_diff(plans, applied=bool(args.apply))
    return 0


if __name__ == "__main__":
    sys.exit(main())
