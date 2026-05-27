from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import BaseModel

if TYPE_CHECKING:
    from app.models.user import User


class CustomerNote(BaseModel):
    """An admin's free-text note about a customer (internal CRM, never customer-facing).

    One row per note. ``user_id`` is the customer the note is *about* (ON DELETE
    CASCADE — notes don't outlive the customer). ``author_id`` is the admin who
    wrote it, ON DELETE SET NULL so a note survives the removal of its author
    (same rationale as the audit/inventory actor FKs); ``author_name`` then
    falls back to a placeholder. Any admin may edit or delete any note (small
    trusted team, every mutation audited as ``resource_type="customer_note"``).
    """

    __tablename__ = "customer_notes"

    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    author_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL")
    )
    body: Mapped[str] = mapped_column(Text)

    user: Mapped["User"] = relationship("User", foreign_keys=[user_id], back_populates="notes")
    author: Mapped["User | None"] = relationship("User", foreign_keys=[author_id])

    def __repr__(self) -> str:
        return f"<CustomerNote user#{self.user_id} by#{self.author_id}>"

    @property
    def author_name(self) -> str:
        """Display name for the authoring admin (first + last initial).

        Relies on ``self.author`` being eager-loaded by the query. Returns a
        placeholder when the author has been removed (``author_id`` is NULL).
        """
        if self.author is None:
            return "Removed admin"
        first = (self.author.first_name or "").strip()
        last = (self.author.last_name or "").strip()
        if first and last:
            return f"{first} {last[0]}."
        return first or self.author.email
