"""Marketing-email suppression list (`email_suppressions`).

One row per email address that must never receive **marketing** mail again.
Transactional mail (order confirmations, password resets, faktura delivery,
net-terms dunning) is deliberately NOT filtered through this table — a customer
who opts out of marketing is still owed the receipt for what they bought.

Why a table keyed by the *address* rather than a flag on ``users``:

- Marketing recipients come from three unrelated shapes — ``users`` (the
  ``all`` / ``customers`` / ``segment`` audiences), ``companies.email`` (the
  ``b2b`` audience) and ``newsletter_subscribers`` (anonymous opt-ins). A
  per-shape opt-out flag would need three separate mechanisms and would still
  leak: the same human can be a user *and* a company contact.
- The unsubscribe link in a campaign has to work for an address with no account
  at all, so the opt-out record cannot hang off ``users``.
- Suppression is the single check every marketing sender consults, so "did we
  respect the opt-out?" has exactly one answer to audit.

The row is append-only in practice (``suppress`` is idempotent). Re-subscribing
deletes it — that is the only removal path, and it requires a fresh, explicit
opt-in.
"""

from __future__ import annotations

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import BaseModel


class EmailSuppression(BaseModel):
    """An address that is opted out of all marketing email.

    ``email`` is stored lowercased (the CRUD layer normalises) and is unique —
    suppressing an already-suppressed address is a no-op, not a duplicate.
    ``reason`` is a short machine-readable label (e.g. ``"unsubscribe_link"``,
    ``"admin"``) kept for audit, never shown to the recipient.
    """

    __tablename__ = "email_suppressions"

    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    reason: Mapped[str] = mapped_column(String(64), default="unsubscribe_link")
