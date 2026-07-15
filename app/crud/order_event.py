"""Order-timeline recorder.

Stages ``OrderEvent`` rows on the caller's session in the same transaction as
the state change that triggered them (mirrors ``AuditWriter._write``, but a
system actor is allowed — ``actor_id=None`` marks webhook/cron events). The
caller owns the commit.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models.order_event import OrderEvent


class CRUDOrderEvent:
    def record(
        self,
        db: Session,
        *,
        order_id: int,
        kind: str,
        actor_id: int | None = None,
        comment: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> OrderEvent:
        """Append one timeline event. Does not flush/commit — the caller owns
        the transaction so the event and its triggering change land together."""
        event = OrderEvent(
            order_id=order_id,
            kind=kind,
            actor_id=actor_id,
            comment=comment,
            payload=payload or {},
        )
        db.add(event)
        return event

    def list_for_order(self, db: Session, *, order_id: int) -> list[OrderEvent]:
        """Every event for an order, newest first (matches the timeline index)."""
        return list(
            db.scalars(
                select(OrderEvent)
                .where(OrderEvent.order_id == order_id)
                .options(selectinload(OrderEvent.actor))
                .order_by(OrderEvent.created_at.desc(), OrderEvent.id.desc())
            )
        )


order_event = CRUDOrderEvent()
