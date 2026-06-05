"""Append-only cookie-consent audit log (`cookie_consents`).

GDPR/ePrivacy require *proof* of the cookie choices a visitor made: we never
UPDATE a consent in place, we INSERT a new immutable row each time the banner is
saved or reopened (same append-only rule as ``user_consents``). The "current"
choice for a visitor is the latest row by ``created_at`` (ties broken by ``id``)
matched on ``anon_id`` (the FE client-id cookie) and/or ``user_id``.

Each row captures the per-category booleans (``necessary`` is always coerced
``true`` server-side — strictly-necessary cookies are exempt from consent), the
Global Privacy Control signal the client sent (``gpc``), the ``policy_version``
in force at the time, and the request ``ip`` / ``user_agent`` so the choice can
be evidenced after the fact. Works for both guests (``user_id`` null) and authed
users (``user_id`` attached when a valid bearer is present).
"""

from typing import TYPE_CHECKING, Any

from sqlalchemy import Boolean, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import BaseModel

if TYPE_CHECKING:
    from app.models.user import User


class CookieConsent(BaseModel):
    """One immutable cookie-consent choice event for a guest or user.

    Never updated — append a new row to change the current value. ``anon_id`` is
    indexed and nullable (the FE client-id cookie, present for guests). ``user_id``
    is nullable and set null on user delete so anonymized accounts keep their
    audit trail. ``categories`` is a JSONB map ``{necessary, analytics, marketing}``
    with ``necessary`` always true.
    """

    __tablename__ = "cookie_consents"

    anon_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), index=True, nullable=True
    )
    categories: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    gpc: Mapped[bool] = mapped_column(Boolean, default=False)
    policy_version: Mapped[str | None] = mapped_column(String(50), nullable=True)
    ip: Mapped[str | None] = mapped_column(String(45))
    user_agent: Mapped[str | None] = mapped_column(String(512))

    user: Mapped["User | None"] = relationship("User", passive_deletes=True)

    def __repr__(self) -> str:
        return f"<CookieConsent anon={self.anon_id} user={self.user_id} {self.categories}>"
