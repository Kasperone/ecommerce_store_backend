"""Pydantic schemas for admin staff management (/api/v1/admin/staff).

A thin management view over the ``users`` table scoped to ADMIN-role accounts:
list the admin roster and PATCH a user's ``role`` / ``is_active``. There is no
create schema — inviting a brand-new admin by email is deferred (an existing
user is promoted via the PATCH instead).

``StaffResponse`` deliberately exposes only the roster-relevant columns (no
shipping/company PII) — it is a strict subset of ``UserResponse``.
"""

from __future__ import annotations

from app.models.user import UserRole
from app.schemas.base import BaseSchema, TimestampSchema


class StaffResponse(TimestampSchema):
    """A staff (admin-or-promotable user) row as the API returns it.

    ``TimestampSchema`` supplies ``id`` / ``created_at`` / ``updated_at``.
    """

    email: str
    first_name: str
    last_name: str
    role: UserRole
    is_active: bool
    is_verified: bool


class StaffUpdate(BaseSchema):
    """Request body for ``PATCH /api/v1/admin/staff/{user_id}`` (all optional).

    Either field may be sent on its own. ``role`` promotes/demotes between
    ``CUSTOMER`` and ``ADMIN``; ``is_active`` toggles login access. The route
    enforces the self-lockout and last-active-admin guards against the merged
    state.
    """

    role: UserRole | None = None
    is_active: bool | None = None


class StaffListResponse(BaseSchema):
    """Paginated staff list."""

    items: list[StaffResponse]
    total: int
    page: int
    page_size: int
    pages: int
