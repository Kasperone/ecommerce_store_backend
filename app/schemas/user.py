from typing import Literal

from pydantic import EmailStr, Field, computed_field, field_validator

from app.core.capabilities import effective_capabilities as _expand_capabilities
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
    # Granular staff permissions (see app/core/capabilities.py). The frontend
    # gates admin UI off these: an owner implicitly holds every capability.
    is_owner: bool = False
    capabilities: list[str] = Field(default_factory=list)

    @computed_field
    @property
    def effective_capabilities(self) -> list[str]:
        """Everything ``capabilities`` actually authorises, expanded.

        ``capabilities`` is the raw stored grant set; a *write* capability
        additionally authorises reads (``IMPLIED_READS`` — see
        ``app/core/capabilities.py``), and those implied reads are deliberately
        never written to the column, so the stored array alone understates what
        the staffer may do. The frontend gates admin UI on membership of this
        set, so it must be expanded server-side: re-deriving the implication
        table in TypeScript would let the two drift silently the first time a
        cross-module read edge is added here.

        Owners are **not** special-cased — they bypass capability checks
        entirely (``require_read``), and the frontend's ``can()`` short-circuits
        on ``is_owner`` the same way, so this stays "what the grants say".
        Sorted for a stable payload.
        """
        return sorted(_expand_capabilities(self.capabilities))

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
