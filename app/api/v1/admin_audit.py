"""Admin audit-log API — a read-only view over ``admin_audit_events``.

Surfaces the audit trail the ``AuditWriter`` records for every admin
POST/PUT/DELETE across products, categories, orders, customers, and inventory.
Read-only: no mutations here, so no audit writer and no commit — just a filtered,
paginated query, admin-gated.
"""

from math import ceil

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.capabilities import AUDIT_READ
from app.core.dependencies import get_db, require_read
from app.crud import audit_event as crud_audit_event
from app.models.admin_audit_event import AdminAuditEvent
from app.models.user import User
from app.schemas.audit_event import AuditEventItem, AuditEventListResponse

router = APIRouter()

_read = Depends(require_read(AUDIT_READ))


def _item(event: AdminAuditEvent) -> AuditEventItem:
    """Build a response row, joining in the acting admin's email for display."""
    return AuditEventItem(
        id=event.id,
        created_at=event.created_at,
        updated_at=event.updated_at,
        actor_id=event.actor_id,
        actor_email=event.actor.email if event.actor else None,
        action=event.action,
        resource_type=event.resource_type,
        resource_id=event.resource_id,
        payload=event.payload,
        ip=event.ip,
        user_agent=event.user_agent,
    )


@router.get("/", response_model=AuditEventListResponse)
async def list_audit_events(
    db: Session = Depends(get_db),
    current_admin: User = _read,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    resource_type: str | None = Query(None, description="Filter by resource type"),
    action: str | None = Query(None, description="Filter by action (create/update/delete)"),
    resource_id: int | None = Query(None, description="Filter to one entity's history"),
):
    """List audit events, newest first, with filters + pagination (admin)."""
    skip = (page - 1) * page_size
    events = crud_audit_event.get_multi_admin(
        db,
        resource_type=resource_type,
        action=action,
        resource_id=resource_id,
        skip=skip,
        limit=page_size,
    )
    total = crud_audit_event.count_admin(
        db, resource_type=resource_type, action=action, resource_id=resource_id
    )
    pages = ceil(total / page_size) if total > 0 else 1
    return {
        "items": [_item(event) for event in events],
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": pages,
    }
