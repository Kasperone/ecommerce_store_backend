from typing import TYPE_CHECKING, Any

from sqlalchemy import ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import BaseModel

if TYPE_CHECKING:
    from app.models.order import Order
    from app.models.user import User


class OrderEvent(BaseModel):
    """Append-only per-order narrative — the order's timeline.

    Distinct from ``admin_audit_events`` (global compliance log of every admin
    mutation): this is the human-readable story of *one* order — system
    milestones (created / paid / status_changed / refunded / tracking_added),
    edits (A2), and free-text staff comments (``kind='note'``). Overlap with the
    audit log is fine and intentional; they answer different questions.

    ``actor_id`` is nullable + SET NULL: ``None`` marks a system event (webhook
    payment, cron), and a deleted staff account doesn't drop the order's history.
    Rows cascade-delete with the order.
    """

    __tablename__ = "order_events"

    order_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("orders.id", ondelete="CASCADE"), index=True
    )
    # created | status_changed | paid | refunded | tracking_added | edited
    # | fulfillment_created (A4 later) | email_sent | note
    kind: Mapped[str] = mapped_column(String(32))
    actor_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    # Free text for kind='note' (a staff comment); NULL for system events.
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Structured context, e.g. {"from": "pending", "to": "paid"} or an edit diff.
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)

    order: Mapped["Order"] = relationship("Order", back_populates="events")
    actor: Mapped["User | None"] = relationship("User", passive_deletes=True)

    __table_args__ = (
        Index(
            "ix_order_events_order_created",
            "order_id",
            text("created_at DESC"),
        ),
    )

    def __repr__(self) -> str:
        return f"<OrderEvent {self.kind} order#{self.order_id} by user#{self.actor_id}>"
