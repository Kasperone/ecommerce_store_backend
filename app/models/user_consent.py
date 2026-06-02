"""Append-only marketing/consent audit log (`user_consents`).

GDPR consent must be *auditable*: we never UPDATE a consent in place, we INSERT a
new row each time the user grants or withdraws one (decision D-B-5 / D-ACCT-3).
The "current" state for a given ``(user_id, consent_type)`` is therefore the
latest row by ``created_at`` (ties broken by ``id``). Each row captures where the
change came from (``source``) plus the request ``ip`` / ``user_agent`` so the
grant/withdraw can be evidenced after the fact.
"""

from typing import TYPE_CHECKING

from sqlalchemy import Boolean, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import BaseModel

if TYPE_CHECKING:
    from app.models.user import User


class UserConsent(BaseModel):
    """One immutable consent grant/withdrawal event for a user.

    Never updated — append a new row to change the current value. ``user_id`` is
    indexed and cascades on user delete. ``consent_type`` is a free-form string key
    (e.g. ``"marketing_email"``, ``"newsletter"``); ``granted`` is the new value.
    """

    __tablename__ = "user_consents"

    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    consent_type: Mapped[str] = mapped_column(String(100))
    granted: Mapped[bool] = mapped_column(Boolean)
    source: Mapped[str | None] = mapped_column(String(100))
    ip: Mapped[str | None] = mapped_column(String(100))
    user_agent: Mapped[str | None] = mapped_column(String(512))

    user: Mapped["User"] = relationship("User", back_populates="consents")

    def __repr__(self) -> str:
        return f"<UserConsent {self.user_id} {self.consent_type}={self.granted}>"
