"""Authentication router: register, login, logout, me, password recovery.

Sessions are opaque and server-side (docs/SECURITY.md §1.2). Login is rate limited per
``(email, ip)``. The session cookie is HttpOnly; a companion non-HttpOnly CSRF cookie
enables the double-submit check on mutations.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import select

from cestaplan_api.config import get_settings
from cestaplan_api.deps import (
    CSRF_COOKIE_NAME,
    SESSION_COOKIE_NAME,
    CurrentUser,
    DbSession,
    get_current_user,
    verify_csrf,
)
from cestaplan_api.models import User, UserSession
from cestaplan_api.schemas.auth import (
    LoginRequest,
    LoginResponse,
    MessageResponse,
    PasswordRecoveryRequest,
    RegisterRequest,
    UserResponse,
)
from cestaplan_api.security import (
    hash_ip,
    hash_password,
    hash_token,
    login_rate_limiter,
    new_csrf_token,
    new_session_token,
    verify_password,
)
from cestaplan_api.services.audit import record_audit

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def _set_session_cookies(response: Response, raw_token: str, csrf_token: str) -> None:
    settings = get_settings()
    max_age = settings.session_ttl_hours * 3600
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=raw_token,
        max_age=max_age,
        httponly=True,
        secure=settings.cookie_secure,
        samesite=settings.cookie_samesite,
        path="/",
    )
    response.set_cookie(
        key=CSRF_COOKIE_NAME,
        value=csrf_token,
        max_age=max_age,
        httponly=False,  # readable by JS so the front can echo it in the header
        secure=settings.cookie_secure,
        samesite=settings.cookie_samesite,
        path="/",
    )


def _clear_session_cookies(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    response.delete_cookie(CSRF_COOKIE_NAME, path="/")


@router.post("/register", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
def register(payload: RegisterRequest, db: DbSession) -> UserResponse:
    """Create an account. Email is normalised to lowercase; duplicates are rejected."""
    email = payload.email.strip().lower()
    exists = db.execute(select(User.id).where(User.email == email)).first()
    if exists is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="El email ya está registrado"
        )

    user = User(
        email=email,
        password_hash=hash_password(payload.password),
        display_name=payload.display_name,
    )
    db.add(user)
    db.flush()
    record_audit(db, action="user.register", actor_user_id=user.id, entity_type="user",
                 entity_public_id=user.public_id)
    return UserResponse.from_model(user)


@router.post("/login", response_model=LoginResponse)
def login(
    payload: LoginRequest, request: Request, response: Response, db: DbSession
) -> LoginResponse:
    """Verify credentials, open an opaque session and set the session + CSRF cookies."""
    email = payload.email.strip().lower()
    ip = _client_ip(request)
    rate_key = f"{email}|{ip or '-'}"

    if login_rate_limiter.is_limited(rate_key):
        record_audit(db, action="auth.login.rate_limited", entity_type="user", ip=ip,
                     metadata={"email": email})
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Demasiados intentos. Inténtalo de nuevo más tarde.",
        )

    user = db.execute(select(User).where(User.email == email)).scalar_one_or_none()
    # Uniform failure whether the user is missing or the password is wrong.
    if user is None or not verify_password(user.password_hash, payload.password):
        login_rate_limiter.record_failure(rate_key)
        record_audit(db, action="auth.login.failed", entity_type="user", ip=ip,
                     metadata={"email": email})
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Credenciales inválidas"
        )
    if user.status != "active":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Cuenta no disponible"
        )

    login_rate_limiter.reset(rate_key)

    now = datetime.now(UTC)
    raw_token, token_hash = new_session_token()
    csrf_token = new_csrf_token()
    session = UserSession(
        user_id=user.id,
        token_hash=token_hash,
        issued_at=now,
        expires_at=now + timedelta(hours=get_settings().session_ttl_hours),
        last_seen_at=now,
        ip_hash=hash_ip(ip),
        user_agent=request.headers.get("user-agent"),
    )
    db.add(session)
    user.last_login_at = now
    record_audit(db, action="auth.login.success", actor_user_id=user.id,
                 entity_type="user", entity_public_id=user.public_id, ip=ip)

    _set_session_cookies(response, raw_token, csrf_token)
    return LoginResponse(user=UserResponse.from_model(user), csrf_token=csrf_token)


@router.post("/logout", response_model=MessageResponse, dependencies=[Depends(verify_csrf)])
def logout(
    request: Request, response: Response, user: CurrentUser, db: DbSession
) -> MessageResponse:
    """Revoke the current session server-side and clear the cookies."""
    raw_token = request.cookies.get(SESSION_COOKIE_NAME)
    if raw_token:
        session = db.execute(
            select(UserSession).where(UserSession.token_hash == hash_token(raw_token))
        ).scalar_one_or_none()
        if session is not None and session.revoked_at is None:
            session.revoked_at = datetime.now(UTC)
    record_audit(db, action="auth.logout", actor_user_id=user.id, entity_type="user",
                 entity_public_id=user.public_id)
    _clear_session_cookies(response)
    return MessageResponse(detail="Sesión cerrada")


@router.get("/me", response_model=UserResponse)
def me(user: CurrentUser) -> UserResponse:
    """Return the authenticated user."""
    return UserResponse.from_model(user)


@router.post("/password-recovery", response_model=MessageResponse)
def request_password_recovery(
    payload: PasswordRecoveryRequest, request: Request, db: DbSession
) -> MessageResponse:
    """Password-recovery request (STUB for the vertical slice).

    The response is intentionally **uniform** whether or not the email exists, so the
    endpoint never discloses account existence (docs/SECURITY.md §1.5). Actual token
    issuance and email delivery are deferred to a later phase; nothing is sent here.
    """
    email = payload.email.strip().lower()
    record_audit(db, action="auth.password_recovery.requested", entity_type="user",
                 ip=_client_ip(request), metadata={"email": email})
    return MessageResponse(
        detail=(
            "Si el email está registrado, recibirás instrucciones para "
            "restablecer la contraseña."
        ),
    )


# Re-exported so main.py can also depend on it if needed.
__all__ = ["get_current_user", "router"]
