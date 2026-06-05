"""Newsletter subscriber CRUD.

Idempotent by design: ``subscribe`` looks up the (lowercased) email and either
creates a fresh row or **reactivates** the existing one — it never inserts a
duplicate. No commit anywhere; the route owns the transaction so the subscriber
row and any appended ``user_consents`` row land together (mirrors
``CRUDUserConsent.record`` / ``CRUDBanner``).

``unsubscribe_token`` is generated with ``secrets.token_urlsafe`` — a reserved
slot for a future token-based unsubscribe link (not wired this wave).
"""

from __future__ import annotations

import secrets

from sqlalchemy.orm import Session

from app.models.newsletter_subscriber import NewsletterSubscriber

# Byte-length for the unsubscribe token; token_urlsafe(32) → ~43 url-safe chars,
# comfortably inside the 128-char column.
_UNSUBSCRIBE_TOKEN_BYTES = 32


def _normalize_email(email: str) -> str:
    return email.strip().lower()


class CRUDNewsletter:
    """Subscriber rows (no commit; caller owns the txn)."""

    def get_by_email(self, db: Session, *, email: str) -> NewsletterSubscriber | None:
        return (
            db.query(NewsletterSubscriber)
            .filter(NewsletterSubscriber.email == _normalize_email(email))
            .first()
        )

    def subscribe(
        self,
        db: Session,
        *,
        email: str,
        source: str | None = None,
        ip: str | None = None,
        user_agent: str | None = None,
    ) -> NewsletterSubscriber:
        """Idempotently opt an email in to the newsletter.

        New email → insert a row. Existing email → reactivate it (set
        ``is_active`` True) and refresh its request metadata. Never creates a
        duplicate row. Caller commits.
        """
        normalized = _normalize_email(email)
        existing = self.get_by_email(db, email=normalized)
        if existing is not None:
            existing.is_active = True
            existing.source = source
            existing.ip = ip
            existing.user_agent = user_agent or None
            db.add(existing)
            return existing

        subscriber = NewsletterSubscriber(
            email=normalized,
            is_active=True,
            source=source,
            ip=ip,
            user_agent=(user_agent or None),
            unsubscribe_token=secrets.token_urlsafe(_UNSUBSCRIBE_TOKEN_BYTES),
        )
        db.add(subscriber)
        return subscriber


newsletter = CRUDNewsletter()
