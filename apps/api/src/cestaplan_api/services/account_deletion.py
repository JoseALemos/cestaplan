"""Supresión de cuenta (art. 17 RGPD) por anonimización irreversible (SEC5).

Se ANONIMIZA en vez de borrar la fila ``user`` porque la auditoría y los datos de hogar la
referencian por id; conservar la fila anonimizada preserva la integridad referencial sin dejar
datos personales. La operación es destructiva e irreversible: la decide el propio usuario con
confirmación explícita (ver el endpoint).

Alcance (documentado para revisión del responsable):
- Sesiones: se revocan todas.
- Cuenta: email/nombre/consentimiento sustituidos por marcadores; contraseña puesta a un valor
  aleatorio inutilizable; ``status='anonymized'``.
- Membresías: se eliminan (el usuario sale de los hogares compartidos).
- Hogares propios: soft-delete SOLO si no queda ningún otro miembro (sus planes/listas dejan de
  usarse). Un hogar propio con OTROS miembros se conserva (dato de terceros) — caso a revisar si
  se quiere forzar transferencia de propiedad.
- Auditoría: se redacta el email guardado en los eventos de este usuario.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from cestaplan_api.models import AuditLog, Household, HouseholdMember, User, UserSession
from cestaplan_api.security import hash_password

_ANON_EMAIL_DOMAIN = "cuenta-eliminada.invalid"


def anonymize_account(db: Session, user: User) -> dict[str, int]:
    """Anonimiza irreversiblemente la cuenta de ``user``. Devuelve un recuento de lo tocado."""
    now = datetime.now(UTC)
    counts = {
        "sessions_revoked": 0,
        "memberships_removed": 0,
        "households_soft_deleted": 0,
        "audit_scrubbed": 0,
    }

    # 1. Revocar todas las sesiones activas.
    sessions = db.execute(
        select(UserSession).where(
            UserSession.user_id == user.id, UserSession.revoked_at.is_(None)
        )
    ).scalars().all()
    for s in sessions:
        s.revoked_at = now
    counts["sessions_revoked"] = len(sessions)

    # 2. Hogares propios: soft-delete si el usuario es el único miembro con cuenta.
    owned = db.execute(
        select(Household).where(
            Household.owner_user_id == user.id, Household.deleted_at.is_(None)
        )
    ).scalars().all()
    for hh in owned:
        others = db.execute(
            select(func.count())
            .select_from(HouseholdMember)
            .where(
                HouseholdMember.household_id == hh.id,
                HouseholdMember.user_id.is_not(None),
                HouseholdMember.user_id != user.id,
            )
        ).scalar_one()
        if others == 0:
            hh.deleted_at = now
            counts["households_soft_deleted"] += 1

    # 3. Eliminar las membresías del usuario (sale de los hogares compartidos).
    memberships = db.execute(
        select(HouseholdMember).where(HouseholdMember.user_id == user.id)
    ).scalars().all()
    for m in memberships:
        db.delete(m)
    counts["memberships_removed"] = len(memberships)

    # 4. Redactar el email en los eventos de auditoría del usuario.
    audits = db.execute(
        select(AuditLog).where(AuditLog.actor_user_id == user.id)
    ).scalars().all()
    for a in audits:
        meta = a.audit_metadata
        if isinstance(meta, dict) and "email" in meta:
            a.audit_metadata = {**meta, "email": "[anonimizado]"}
            counts["audit_scrubbed"] += 1

    # 5. Anonimizar la PII de la cuenta (la fila se conserva por integridad referencial).
    user.email = f"anon+{user.public_id}@{_ANON_EMAIL_DOMAIN}"
    user.display_name = None
    user.password_hash = hash_password(uuid.uuid4().hex)  # aleatorio: nadie conoce la contraseña
    user.ai_consent_at = None
    user.email_verified_at = None
    user.status = "anonymized"
    db.flush()
    return counts
