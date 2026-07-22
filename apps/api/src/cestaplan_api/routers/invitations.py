"""Invitation-acceptance router.

Split from the households router because acceptance is addressed by the raw invitation
*token* (``/api/v1/invitations/{token}/accept``) rather than by household id, so it needs
its own prefix. Authorization is the token itself plus an email match: the logged-in
user's email must equal the invited email. Only the token's hash is ever compared against
the database — the raw token never touches storage or logs (see
:func:`cestaplan_api.security.hash_token`).
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select

from cestaplan_api.deps import CurrentUser, DbSession, verify_csrf
from cestaplan_api.models import HouseholdInvitation, HouseholdMember
from cestaplan_api.schemas.household import (
    AcceptInvitationResponse,
    InvitationPreviewResponse,
)
from cestaplan_api.security import hash_token
from cestaplan_api.services.audit import record_audit

router = APIRouter(prefix="/api/v1/invitations", tags=["invitations"])


def _effective_status(invitation: HouseholdInvitation, now: datetime) -> str:
    """Status as seen by a reader: a still-pending row past its expiry reads as expired."""
    if invitation.status == "pending" and invitation.expires_at <= now:
        return "expired"
    return invitation.status


@router.get("/{token}", response_model=InvitationPreviewResponse)
def preview_invitation(
    token: str,
    user: CurrentUser,
    db: DbSession,
) -> InvitationPreviewResponse:
    """Render-only preview of an invitation for the accept page (authenticated).

    Requires a logged-in user (so the accept flow can send them through /login first) but
    not an email match — it reports ``email_matches`` so the UI can warn the wrong account
    before attempting to accept. Never returns the token.
    """
    invitation = db.execute(
        select(HouseholdInvitation).where(
            HouseholdInvitation.token_hash == hash_token(token)
        )
    ).scalar_one_or_none()
    if invitation is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Invitación no encontrada"
        )

    now = datetime.now(UTC)
    return InvitationPreviewResponse(
        household_name=invitation.household.name,
        email=invitation.email,
        role=invitation.role,
        status=_effective_status(invitation, now),
        email_matches=(user.email or "").strip().lower() == invitation.email,
    )


@router.post(
    "/{token}/accept",
    response_model=AcceptInvitationResponse,
    dependencies=[Depends(verify_csrf)],
)
def accept_invitation(
    token: str,
    user: CurrentUser,
    db: DbSession,
) -> AcceptInvitationResponse:
    """Accept an invitation and join its household with the invited role.

    Validates the token (by hash), that it is still pending and not expired, and that the
    logged-in user's email matches the invited email. Creates a linked
    :class:`HouseholdMember`. Idempotency: if the caller is already a member, returns 409.
    """
    invitation = db.execute(
        select(HouseholdInvitation).where(
            HouseholdInvitation.token_hash == hash_token(token)
        )
    ).scalar_one_or_none()
    if invitation is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Invitación no encontrada"
        )

    now = datetime.now(UTC)

    # An expired-by-time but still "pending" row is treated as expired (and marked so).
    if invitation.status == "pending" and invitation.expires_at <= now:
        invitation.status = "expired"
        db.flush()
    if invitation.status == "expired" or (
        invitation.status == "pending" and invitation.expires_at <= now
    ):
        raise HTTPException(
            status_code=status.HTTP_410_GONE, detail="La invitación ha caducado"
        )
    if invitation.status != "pending":
        # revoked or already accepted.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="La invitación ya no está disponible",
        )

    # The accepting account's email must match the invited email exactly.
    if (user.email or "").strip().lower() != invitation.email:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Esta invitación es para otra cuenta de correo",
        )

    household = invitation.household

    existing_member = db.execute(
        select(HouseholdMember.id).where(
            HouseholdMember.household_id == invitation.household_id,
            HouseholdMember.user_id == user.id,
        )
    ).first()
    if existing_member is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Ya eres miembro de este hogar"
        )

    member = HouseholdMember(
        household_id=invitation.household_id,
        user_id=user.id,
        role=invitation.role,
        display_name=user.display_name,
        is_eater=True,
        joined_at=now,
    )
    db.add(member)

    invitation.status = "accepted"
    invitation.accepted_at = now
    invitation.accepted_user_id = user.id
    db.flush()

    record_audit(db, action="household.invitation.accept", actor_user_id=user.id,
                 household_id=invitation.household_id, entity_type="household_member",
                 entity_public_id=member.public_id)
    return AcceptInvitationResponse(
        household_id=household.public_id,
        household_name=household.name,
        role=invitation.role,
    )
