from pydantic import EmailStr, Field, field_validator
from typing import Optional
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
