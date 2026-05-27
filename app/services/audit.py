"""Admin audit-log writer.

Records every admin POST/PUT/DELETE into ``admin_audit_events`` in the same
transaction as the data change. The writer never opens its own session or
commits — the route owns the transaction boundary so the mutation and the
audit row land (or roll back) together.
"""

from typing import Any

from fastapi import Depends, Request
from fastapi.encoders import jsonable_encoder
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import get_history

from app.core.dependencies import client_ip, get_current_admin, get_db, user_agent
from app.models.admin_audit_event import AdminAuditEvent
from app.models.base import BaseModel as ORMBase
from app.models.user import User


def _snapshot(obj: ORMBase) -> dict[str, Any]:
    """Serialize an ORM object to a JSON-safe dict of its column attributes."""
    return {
        attr.key: jsonable_encoder(getattr(obj, attr.key)) for attr in obj.__mapper__.column_attrs
    }


def _diff(obj: ORMBase) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return (before, after) dicts of fields changed on ``obj``.

    Uses SQLAlchemy ``get_history`` which is queryable only between attribute
    mutation and ``Session.flush()``. The caller must invoke this after the
    CRUD layer has set fields and before ``Session.commit()``.
    """
    before: dict[str, Any] = {}
    after: dict[str, Any] = {}
    for attr in obj.__mapper__.column_attrs:
        hist = get_history(obj, attr.key)
        if not hist.has_changes():
            continue
        before[attr.key] = jsonable_encoder(hist.deleted[0]) if hist.deleted else None
        after[attr.key] = jsonable_encoder(hist.added[0]) if hist.added else None
    return before, after


class AuditWriter:
    """Stages ``AdminAuditEvent`` rows on the request's session.

    Construct one per request via ``Depends(get_audit_writer)``. Do not call
    flush or commit — the route handler does that as part of its own
    transaction.
    """

    def __init__(
        self,
        db: Session,
        actor: User,
        ip: str | None,
        ua: str | None,
    ) -> None:
        self.db = db
        self.actor = actor
        self.ip = ip
        self.ua = ua

    def record_create(self, *, resource_type: str, obj: ORMBase) -> None:
        """Record a successful create. ``obj.id`` must already be populated."""
        self._write(
            action="create",
            resource_type=resource_type,
            resource_id=getattr(obj, "id", None),
            payload={"after": _snapshot(obj)},
        )

    def record_update(self, *, resource_type: str, obj: ORMBase) -> None:
        """Record an update. Writes the row even on no-op PUTs (empty diff)
        so the audit log captures admin intent, not just data movement."""
        before, after = _diff(obj)
        self._write(
            action="update",
            resource_type=resource_type,
            resource_id=getattr(obj, "id", None),
            payload={"before": before, "after": after},
        )

    def record_delete(self, *, resource_type: str, obj: ORMBase) -> None:
        """Record a delete. Must be called before the CRUD delete so ``obj``
        is still readable for the snapshot."""
        self._write(
            action="delete",
            resource_type=resource_type,
            resource_id=getattr(obj, "id", None),
            payload={"before": _snapshot(obj)},
        )

    def record_event(
        self,
        *,
        action: str,
        resource_type: str,
        resource_id: int | None,
        payload: dict[str, Any],
    ) -> None:
        """Record an event with a caller-supplied payload.

        For mutations that have no clean ORM-object diff — e.g. attaching or
        detaching a row from a many-to-many join (a tag ↔ customer link) — where
        ``record_create/update/delete`` (which read column attrs / ``get_history``)
        don't apply. The caller describes the change explicitly.
        """
        self._write(
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            payload=payload,
        )

    def _write(
        self,
        *,
        action: str,
        resource_type: str,
        resource_id: int | None,
        payload: dict[str, Any],
    ) -> None:
        row = AdminAuditEvent(
            actor_id=self.actor.id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            payload=payload,
            ip=self.ip,
            user_agent=self.ua,
        )
        self.db.add(row)


def get_audit_writer(
    request: Request,
    db: Session = Depends(get_db),
    actor: User = Depends(get_current_admin),
) -> AuditWriter:
    """FastAPI dependency: per-request writer bound to the admin actor.

    Re-uses ``get_current_admin`` which FastAPI dedupes within a request, so
    declaring this dependency on a route does not re-run the admin check.
    """
    return AuditWriter(
        db=db,
        actor=actor,
        ip=client_ip(request),
        ua=user_agent(request),
    )
