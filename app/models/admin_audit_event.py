from typing import TYPE_CHECKING, Any

from sqlalchemy import ForeignKey, Index, Integer, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import BaseModel

if TYPE_CHECKING:
    from app.models.user import User


class AdminAuditEvent(BaseModel):
    """Append-only record of admin POST/PUT/DELETE mutations.

    Written in the same transaction as the data change so the audit row
    and the mutation succeed or fail together. ``actor_id`` is set null on
    user delete so deleted accounts don't drop history; ``resource_id`` is
    nullable to reserve the column for future failed-attempt logging.
    """

    __tablename__ = "admin_audit_events"

    actor_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    action: Mapped[str] = mapped_column(String(64))
    resource_type: Mapped[str] = mapped_column(String(64))
    resource_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    ip: Mapped[str | None] = mapped_column(String(45))
    user_agent: Mapped[str | None] = mapped_column(String(500))

    actor: Mapped["User | None"] = relationship("User", passive_deletes=True)

    __table_args__ = (
        Index(
            "ix_admin_audit_events_resource",
            "resource_type",
            "resource_id",
            text("created_at DESC"),
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<AdminAuditEvent {self.action} {self.resource_type}"
            f"#{self.resource_id} by user#{self.actor_id}>"
        )
