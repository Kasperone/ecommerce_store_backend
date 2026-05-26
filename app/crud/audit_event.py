"""Read-only CRUD for the admin audit log.

Audit rows are *written* by ``app/services/audit.py::AuditWriter`` as a
side-effect of admin mutations; this layer only reads them for the
``/admin/audit-events`` list view — filtered, newest-first, with the acting
admin eager-loaded so the list can show the actor's email without an N+1.
"""

from __future__ import annotations

from sqlalchemy.orm import Session, selectinload

from app.models.admin_audit_event import AdminAuditEvent


class CRUDAuditEvent:
    """Filtered, paginated reads over ``admin_audit_events`` (no writes here)."""

    def _filtered_query(
        self,
        db: Session,
        *,
        resource_type: str | None,
        action: str | None,
        resource_id: int | None,
    ):
        query = db.query(AdminAuditEvent)
        if resource_type is not None:
            query = query.filter(AdminAuditEvent.resource_type == resource_type)
        if action is not None:
            query = query.filter(AdminAuditEvent.action == action)
        if resource_id is not None:
            query = query.filter(AdminAuditEvent.resource_id == resource_id)
        return query

    def get_multi_admin(
        self,
        db: Session,
        *,
        resource_type: str | None = None,
        action: str | None = None,
        resource_id: int | None = None,
        skip: int = 0,
        limit: int = 20,
    ) -> list[AdminAuditEvent]:
        """Audit events matching the filters, newest first, actor eager-loaded."""
        return (
            self._filtered_query(
                db, resource_type=resource_type, action=action, resource_id=resource_id
            )
            .options(selectinload(AdminAuditEvent.actor))
            .order_by(AdminAuditEvent.created_at.desc(), AdminAuditEvent.id.desc())
            .offset(skip)
            .limit(limit)
            .all()
        )

    def count_admin(
        self,
        db: Session,
        *,
        resource_type: str | None = None,
        action: str | None = None,
        resource_id: int | None = None,
    ) -> int:
        return self._filtered_query(
            db, resource_type=resource_type, action=action, resource_id=resource_id
        ).count()


audit_event = CRUDAuditEvent()
