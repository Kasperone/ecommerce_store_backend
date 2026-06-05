"""Newsletter subscriber model (`newsletter_subscribers`).

A single row per email that has opted in to the marketing newsletter via the
public footer form. This table is the **anonymous-friendly** counterpart to the
``user_consents`` audit log: when a subscriber's email maps to a registered
user we ALSO append a ``user_consents`` newsletter row (the proper GDPR audit
trail), but for an email with no account this row IS the consent record — it
captures the ``source``, request ``ip`` / ``user_agent`` and an ``is_active``
flag so an opt-out can park the row without deleting it.

``unsubscribe_token`` is a reserved one-time secret (``secrets.token_urlsafe``)
for a future token-based unsubscribe link; ``confirmed_at`` is a reserved
nullable slot for a future double-opt-in confirmation flow. Neither is wired
this wave (single opt-in record only).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import BaseModel


class NewsletterSubscriber(BaseModel):
    """One newsletter opt-in, keyed by a unique (lowercased) email.

    ``is_active`` lets an unsubscribe park the row instead of deleting it (so a
    re-subscribe reactivates the same row rather than creating a duplicate).
    ``source`` records where the opt-in came from (e.g. ``"footer"``). ``ip`` /
    ``user_agent`` are best-effort request metadata for anonymous subscribers.
    """

    __tablename__ = "newsletter_subscribers"

    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    source: Mapped[str | None] = mapped_column(String(100))
    ip: Mapped[str | None] = mapped_column(String(100))
    user_agent: Mapped[str | None] = mapped_column(String(512))
    unsubscribe_token: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime)

    def __repr__(self) -> str:
        return f"<NewsletterSubscriber {self.email!r} active={self.is_active}>"
