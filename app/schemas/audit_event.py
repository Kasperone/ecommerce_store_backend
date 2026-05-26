"""Admin audit-log read schemas — powers the `/admin/audit-events` list view.

``actor_email`` is a plain ``str`` (not ``EmailStr``) so serializing it never
re-runs email-validator on the way out. ``payload`` is the raw ``{before?, after?}``
JSON the ``AuditWriter`` recorded; the frontend renders it as a diff.
"""

from typing import Any

from app.schemas.base import BaseSchema, TimestampSchema


class AuditEventItem(TimestampSchema):
    """One audit-log row, with the acting admin's email joined in for display."""

    actor_id: int | None = None
    actor_email: str | None = None
    action: str
    resource_type: str
    resource_id: int | None = None
    payload: dict[str, Any]
    ip: str | None = None
    user_agent: str | None = None


class AuditEventListResponse(BaseSchema):
    """Paginated admin audit log."""

    items: list[AuditEventItem]
    total: int
    page: int
    page_size: int
    pages: int
