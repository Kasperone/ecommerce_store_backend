"""Admin-facing customer schemas (a read/manage view over the users table).

These power `/api/v1/admin/customers`. Distinct from `UserResponse` (self-service
`/auth/me`) because the admin view adds lockout state and lifetime order metrics.
"""

from datetime import datetime

from pydantic import Field

from app.models.user import UserRole
from app.schemas.base import BaseSchema, TimestampSchema
from app.schemas.tag import TagChip
from app.schemas.user import UserResponse


class CustomerOrderMetrics(BaseSchema):
    """Lifetime order aggregates for a customer.

    ``total_spent`` is keyed by currency and **excludes cancelled orders**.
    ``order_count`` / ``last_order_at`` count every order regardless of status.
    """

    order_count: int
    total_spent: dict[str, float]
    last_order_at: datetime | None = None


class CustomerListItem(TimestampSchema):
    """Lightweight customer row for the admin list."""

    email: str
    first_name: str
    last_name: str
    role: UserRole
    is_active: bool
    is_verified: bool
    is_locked: bool
    company_name: str | None = None
    shipping_country: str | None = None
    order_count: int = 0
    note_count: int = 0
    tags: list[TagChip] = Field(default_factory=list)


class CustomerDetail(UserResponse):
    """Full customer record + lockout state + lifetime metrics + tags."""

    is_locked: bool
    locked_until: datetime | None = None
    metrics: CustomerOrderMetrics
    tags: list[TagChip] = Field(default_factory=list)


class CustomerAdminUpdate(BaseSchema):
    """Admin PATCH body — toggle verification / active state."""

    is_verified: bool | None = None
    is_active: bool | None = None


class CustomerListResponse(BaseSchema):
    """Paginated admin customer list."""

    items: list[CustomerListItem]
    total: int
    page: int
    page_size: int
    pages: int
