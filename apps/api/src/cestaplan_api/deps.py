"""FastAPI dependencies: authentication, household authorization and CSRF.

- :func:`get_current_user` resolves the opaque session cookie to a live ``User`` or 401.
- :func:`get_household_context` resolves a household by its public UUID and verifies the
  authenticated user's membership (no IDOR: authorization is by membership+role checked
  on the server, never by a client-supplied id). Non-members get 404 so the API does not
  leak the existence of a household.
- :data:`require_editor` / :data:`require_owner` enforce roles per docs/SECURITY.md §3.
- :func:`verify_csrf` implements a stateless double-submit CSRF check for mutations.
"""

from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from cestaplan_api.db import get_db
from cestaplan_api.models import Household, HouseholdMember, User, UserSession
from cestaplan_api.security import hash_token

# Cookie / header names. The session cookie is HttpOnly (set in the auth router); the
# CSRF cookie is readable by JS so the front can echo it back in the header.
SESSION_COOKIE_NAME = "cestaplan_session"
CSRF_COOKIE_NAME = "cestaplan_csrf"
CSRF_HEADER_NAME = "X-CSRF-Token"

DbSession = Annotated[Session, Depends(get_db)]


def _now() -> datetime:
    return datetime.now(UTC)


# --------------------------------------------------------------------------- #
# Authentication
# --------------------------------------------------------------------------- #
def get_current_user(request: Request, db: DbSession) -> User:
    """Resolve the session cookie to a non-expired, non-revoked user, or raise 401."""
    raw_token = request.cookies.get(SESSION_COOKIE_NAME)
    if not raw_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="No autenticado"
        )

    session = db.execute(
        select(UserSession).where(UserSession.token_hash == hash_token(raw_token))
    ).scalar_one_or_none()

    now = _now()
    if (
        session is None
        or session.revoked_at is not None
        or session.expires_at <= now
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Sesión inválida o expirada"
        )

    user = db.get(User, session.user_id)
    if user is None or user.status != "active":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Cuenta no disponible"
        )

    session.last_seen_at = now
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


# --------------------------------------------------------------------------- #
# Household authorization
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class HouseholdContext:
    """The resolved household plus the caller's membership within it."""

    household: Household
    member: HouseholdMember

    @property
    def role(self) -> str:
        return self.member.role


def get_household_context(
    household_id: uuid.UUID, user: CurrentUser, db: DbSession
) -> HouseholdContext:
    """Load a household by public UUID and verify the caller is a member.

    Returns 404 for a missing/deleted household *and* for a household the caller does
    not belong to, so membership is never disclosed to outsiders.
    """
    household = db.execute(
        select(Household).where(Household.public_id == household_id)
    ).scalar_one_or_none()
    if household is None or household.deleted_at is not None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Hogar no encontrado")

    member = db.execute(
        select(HouseholdMember).where(
            HouseholdMember.household_id == household.id,
            HouseholdMember.user_id == user.id,
        )
    ).scalar_one_or_none()
    if member is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Hogar no encontrado")

    return HouseholdContext(household=household, member=member)


HouseholdCtx = Annotated[HouseholdContext, Depends(get_household_context)]


def require_household_role(*allowed: str):
    """Build a dependency asserting the caller's household role is in ``allowed``."""

    def _dependency(ctx: HouseholdCtx) -> HouseholdContext:
        if ctx.member.role not in allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Permisos insuficientes para esta acción",
            )
        return ctx

    return _dependency


# owner+editor may mutate planning/data; only owner may manage members and the household.
require_editor = require_household_role("owner", "editor")
require_owner = require_household_role("owner")

# Convenience annotated dependencies for routes that require a given role.
HouseholdCtxEditor = Annotated[HouseholdContext, Depends(require_editor)]
HouseholdCtxOwner = Annotated[HouseholdContext, Depends(require_owner)]


# --------------------------------------------------------------------------- #
# CSRF (stateless double-submit)
# --------------------------------------------------------------------------- #
def verify_csrf(request: Request) -> None:
    """Reject a mutating request whose CSRF header does not match its CSRF cookie.

    Double-submit pattern: on login the server sets a non-HttpOnly ``cestaplan_csrf``
    cookie and returns the same value in the body. The front echoes it in the
    ``X-CSRF-Token`` header. A cross-site attacker can neither read the cookie
    (SameSite + cross-origin restrictions) nor set the header, so a forged request
    fails this check. Read-only requests (GET/HEAD/OPTIONS) never call this dependency.
    """
    cookie_token = request.cookies.get(CSRF_COOKIE_NAME)
    header_token = request.headers.get(CSRF_HEADER_NAME)
    if not cookie_token or not header_token:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Token CSRF ausente"
        )
    if not secrets.compare_digest(cookie_token, header_token):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Token CSRF inválido"
        )
