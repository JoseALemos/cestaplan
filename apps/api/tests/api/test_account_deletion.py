"""SEC5: anonymize_account (supresión de cuenta art. 17) — servicio."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from cestaplan_api.models import (
    AuditLog,
    Household,
    HouseholdMember,
    User,
    UserSession,
)
from cestaplan_api.security import hash_password
from cestaplan_api.services.account_deletion import anonymize_account


def _user(db: Session, email: str, status: str = "active") -> User:
    u = User(email=email, password_hash=hash_password("x"), display_name="Nombre", status=status)
    db.add(u)
    db.flush()
    return u


def test_anonymize_account_full(db_session: Session) -> None:
    victim = _user(db_session, "victim@example.com")
    other = _user(db_session, "other@example.com")

    now = datetime.now(UTC)
    db_session.add(UserSession(
        user_id=victim.id, token_hash=b"tok", issued_at=now, expires_at=now + timedelta(days=1),
    ))
    solo = Household(name="Solo", owner_user_id=victim.id, currency="EUR")
    shared = Household(name="Compartido", owner_user_id=victim.id, currency="EUR")
    db_session.add_all([solo, shared])
    db_session.flush()
    db_session.add_all([
        HouseholdMember(household_id=solo.id, user_id=victim.id, role="owner", joined_at=now),
        HouseholdMember(household_id=shared.id, user_id=victim.id, role="owner", joined_at=now),
        HouseholdMember(household_id=shared.id, user_id=other.id, role="editor", joined_at=now),
    ])
    db_session.add(AuditLog(
        actor_user_id=victim.id, action="auth.login.failed",
        audit_metadata={"email": "victim@example.com"}, occurred_at=now,
    ))
    db_session.flush()

    counts = anonymize_account(db_session, victim)
    db_session.flush()

    # Cuenta anonimizada.
    assert victim.status == "anonymized"
    assert victim.email.startswith("anon+") and victim.email.endswith("cuenta-eliminada.invalid")
    assert victim.display_name is None
    assert victim.ai_consent_at is None
    # Sesión revocada.
    assert counts["sessions_revoked"] == 1
    sess = db_session.query(UserSession).filter_by(user_id=victim.id).one()
    assert sess.revoked_at is not None
    # Hogar en solitario soft-deleted; compartido conservado.
    db_session.refresh(solo)
    db_session.refresh(shared)
    assert solo.deleted_at is not None
    assert shared.deleted_at is None
    assert counts["households_soft_deleted"] == 1
    # Membresías del usuario eliminadas (las de 'other' se conservan).
    assert counts["memberships_removed"] == 2
    remaining = db_session.query(HouseholdMember).filter_by(user_id=victim.id).all()
    assert remaining == []
    assert db_session.query(HouseholdMember).filter_by(user_id=other.id).count() == 1
    # Email de auditoría redactado.
    assert counts["audit_scrubbed"] == 1
    audit = db_session.query(AuditLog).filter_by(actor_user_id=victim.id).first()
    assert audit.audit_metadata["email"] == "[anonimizado]"
