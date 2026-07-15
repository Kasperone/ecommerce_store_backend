"""Pydantic schemas for admin staff management (/api/v1/admin/staff).

A thin management view over the ``users`` table scoped to ADMIN-role accounts:
list the admin roster and PATCH a user's ``role`` / ``is_active``. There is no
create schema — inviting a brand-new admin by email is deferred (an existing
user is promoted via the PATCH instead).

``StaffResponse`` deliberately exposes only the roster-relevant columns (no
shipping/company PII) — it is a strict subset of ``UserResponse``.
"""

from __future__ import annotations

from pydantic import field_validator

from app.core.capabilities import CAPABILITIES
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
    # Granular permissions (see app/core/capabilities.py). ``is_owner`` ⇒ every
    # capability implicitly; ``capabilities`` is the explicit scoped-staffer set.
    is_owner: bool
    capabilities: list[str]


class StaffUpdate(BaseSchema):
    """Request body for ``PATCH /api/v1/admin/staff/{user_id}`` (all optional).

    Any field may be sent on its own. ``role`` promotes/demotes between
    ``CUSTOMER`` and ``ADMIN``; ``is_active`` toggles login access;
    ``is_owner`` grants/revokes the implicit-all-capabilities owner flag;
    ``capabilities`` sets the explicit scoped-staffer grant set (validated ⊆
    the closed taxonomy). The route enforces the self-lockout,
    last-active-admin, last-owner, and privilege-escalation guards against the
    merged state.
    """

    role: UserRole | None = None
    is_active: bool | None = None
    is_owner: bool | None = None
    capabilities: list[str] | None = None

    @field_validator("capabilities")
    @classmethod
    def validate_capabilities(cls, v: list[str] | None) -> list[str] | None:
        """Reject any capability outside the closed taxonomy; dedupe the rest."""
        if v is None:
            return v
        unknown = sorted(set(v) - CAPABILITIES)
        if unknown:
            raise ValueError(f"Unknown capabilities: {', '.join(unknown)}")
        return sorted(set(v))


class StaffListResponse(BaseSchema):
    """Paginated staff list."""

    items: list[StaffResponse]
    total: int
    page: int
    page_size: int
    pages: int
