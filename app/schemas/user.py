from typing import Literal

from pydantic import EmailStr, Field, field_validator

from app.models.user import UserRole
from app.schemas.base import BaseSchema, TimestampSchema

# Languages we localize transactional emails into. Restricting at the schema
# boundary keeps a clean OpenAPI enum and rejects anything else with a 422.
Locale = Literal["en", "pl"]


class UserBase(BaseSchema):
    """Base user schema with common fields"""

    email: EmailStr
    first_name: str
    last_name: str
    phone: str | None = None
    # Preferred language for transactional emails; optional at registration.
    locale: Locale = "en"


class UserCreate(UserBase):
    """Schema for user registration"""

    password: str = Field(..., min_length=8, max_length=100)

    @field_validator("password")
    @classmethod
    def validate_password(cls, v: str) -> str:
        """Validate password strength"""
        if not any(char.isdigit() for char in v):
            raise ValueError("Password must contain at least one digit")
        if not any(char.isupper() for char in v):
            raise ValueError("Password must contain at least one uppercase letter")
        return v


class UserUpdate(BaseSchema):
    """Schema for updating user profile"""

    first_name: str | None = None
    last_name: str | None = None
    phone: str | None = None
    locale: Locale | None = None

    # Optional company/invoice data
    company_name: str | None = None
    company_tax_id: str | None = None
    company_address_street: str | None = None
    company_address_city: str | None = None
    company_address_postal_code: str | None = None
    company_address_country: str | None = None
    company_address_state: str | None = None


class UserResponse(UserBase, TimestampSchema):
    """Schema for user response (public data)"""

    is_active: bool
    is_verified: bool
    role: UserRole

    # Optional company/invoice data
    company_name: str | None = None
    company_tax_id: str | None = None
    company_address_street: str | None = None
    company_address_city: str | None = None
    company_address_postal_code: str | None = None
    company_address_country: str | None = None
    company_address_state: str | None = None


class UserInDB(UserResponse):
    """Schema for user in database (includes password hash)"""

    hashed_password: str


class UserLogin(BaseSchema):
    """Schema for user login"""

    email: EmailStr
    password: str
