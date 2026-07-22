"""Auth request/response schemas."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, EmailStr, Field

# Password policy (docs/SECURITY.md §1.1): a reasonable minimum length, no arbitrary
# composition rules. Upper bound guards against Argon2 DoS via huge inputs.
_PASSWORD_MIN = 10
_PASSWORD_MAX = 128


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=_PASSWORD_MIN, max_length=_PASSWORD_MAX)
    display_name: str | None = Field(default=None, max_length=200)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=_PASSWORD_MAX)


class PasswordRecoveryRequest(BaseModel):
    email: EmailStr


class UserResponse(BaseModel):
    id: uuid.UUID
    email: EmailStr
    display_name: str | None
    locale: str
    status: str
    created_at: datetime

    @classmethod
    def from_model(cls, user) -> UserResponse:
        return cls(
            id=user.public_id,
            email=user.email,
            display_name=user.display_name,
            locale=user.locale,
            status=user.status,
            created_at=user.created_at,
        )


class LoginResponse(BaseModel):
    """Login result. ``csrf_token`` must be echoed in the ``X-CSRF-Token`` header on
    subsequent mutating requests (see :func:`cestaplan_api.deps.verify_csrf`)."""

    user: UserResponse
    csrf_token: str


class MessageResponse(BaseModel):
    detail: str
