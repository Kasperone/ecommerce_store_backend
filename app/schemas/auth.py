from datetime import datetime

from pydantic import EmailStr, Field, field_validator

from app.schemas.base import BaseSchema


def _validate_password_strength(v: str) -> str:
    """Shared password strength rules. Mirrors UserCreate.validate_password."""
    if not any(char.isdigit() for char in v):
        raise ValueError("Password must contain at least one digit")
    if not any(char.isupper() for char in v):
        raise ValueError("Password must contain at least one uppercase letter")
    return v


class Token(BaseSchema):
    """Schema for JWT token response"""

    access_token: str
    token_type: str = "bearer"


class TokenData(BaseSchema):
    """Schema for token payload data"""

    user_id: int
    email: str
    role: str


class PasswordChange(BaseSchema):
    """Schema for authenticated password change."""

    current_password: str
    new_password: str = Field(..., min_length=8, max_length=100)

    @field_validator("new_password")
    @classmethod
    def validate_new_password(cls, v: str) -> str:
        return _validate_password_strength(v)


class PasswordReset(BaseSchema):
    """Schema for password reset request (step 1: request email)."""

    email: EmailStr


class PasswordResetConfirm(BaseSchema):
    """Schema for password reset confirm (step 2: submit token + new password)."""

    token: str
    new_password: str = Field(..., min_length=8, max_length=100)

    @field_validator("new_password")
    @classmethod
    def validate_new_password(cls, v: str) -> str:
        return _validate_password_strength(v)


class EmailVerification(BaseSchema):
    """Schema for email verification"""

    token: str


class EmailResend(BaseSchema):
    """Schema for resending verification email"""

    email: str


class GenericMessage(BaseSchema):
    """Generic single-message response (used where revealing detail would leak info)."""

    message: str


class SessionResponse(BaseSchema):
    """A single active login session (one ``refresh_tokens`` row).

    ``current`` is True for the row whose ``jti`` matches the caller's refresh
    cookie. It is computed in the route handler, not derived from the ORM model.
    """

    id: int
    created_at: datetime
    created_ip: str | None
    user_agent: str | None
    current: bool
    expires_at: datetime


class BusinessRegister(BaseSchema):
    """Request body for ``POST /api/v1/auth/register-business`` (Wave D / D-D-5).

    Open B2B self-signup: the personal account fields (mirroring the normal
    ``register`` — email + strong password + name) plus the minimal company
    identity (``company_name`` required; ``company_tax_id`` + an optional company
    address). One call
    creates the user (``UserRole.CUSTOMER``) + a ``pending`` company + an active
    ``COMPANY_ADMIN`` membership; the company stays ``pending`` until an admin
    approves it. The user still goes through email verification, and the response
    is anti-enumeration (same generic body whether the email is free or taken).
    """

    email: EmailStr
    password: str = Field(..., min_length=8, max_length=100)
    first_name: str = Field(..., min_length=1, max_length=255)
    last_name: str = Field(..., min_length=1, max_length=255)
    phone: str | None = None
    company_name: str = Field(..., min_length=1, max_length=255)
    company_tax_id: str | None = Field(default=None, max_length=64)
    # Optional company address — persisted on the user (the invoice PDF reads
    # ``users.company_address_*``); the company billing address lives separately
    # on ``companies.billing_address_*``, managed via the company portal.
    company_address_street: str | None = Field(default=None, max_length=255)
    company_address_city: str | None = Field(default=None, max_length=100)
    company_address_postal_code: str | None = Field(default=None, max_length=20)
    company_address_country: str | None = Field(default=None, max_length=100)
    company_address_state: str | None = Field(default=None, max_length=100)

    @field_validator("password")
    @classmethod
    def validate_password(cls, v: str) -> str:
        return _validate_password_strength(v)

    @field_validator("first_name", "last_name", "company_name")
    @classmethod
    def _strip_required(cls, v: str) -> str:
        stripped = v.strip()
        if not stripped:
            raise ValueError("must not be blank")
        return stripped
