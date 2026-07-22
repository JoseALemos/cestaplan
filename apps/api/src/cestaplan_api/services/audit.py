"""Audit logging helper.

Records sensitive actions (denied access, role changes, member management, auth events)
per docs/SECURITY.md §3.2 and docs/PRIVACY.md §7. Never stores secrets, passwords or
raw tokens; IPs are stored hashed.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from cestaplan_api.models import AuditLog
from cestaplan_api.security import hash_ip


def record_audit(
    db: Session,
    *,
    action: str,
    actor_user_id: int | None = None,
    household_id: int | None = None,
    entity_type: str | None = None,
    entity_public_id: uuid.UUID | None = None,
    metadata: dict | None = None,
    ip: str | None = None,
) -> None:
    """Append an :class:`AuditLog` row within the caller's transaction."""
    db.add(
        AuditLog(
            actor_user_id=actor_user_id,
            household_id=household_id,
            action=action,
            entity_type=entity_type,
            entity_public_id=entity_public_id,
            audit_metadata=metadata,
            ip_hash=hash_ip(ip),
            occurred_at=datetime.now(UTC),
        )
    )
