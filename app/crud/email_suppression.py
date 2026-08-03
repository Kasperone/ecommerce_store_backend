"""Marketing-email suppression CRUD.

No commit anywhere (the route owns the transaction), matching ``CRUDNewsletter``
/ ``CRUDCoupon``. Addresses are normalised to lowercase on every read and write
so ``Foo@Example.com`` and ``foo@example.com`` are the same subject — the same
normalisation ``CRUDNewsletter`` applies, because the two tables are keyed by
the same human.

Every marketing sender MUST call ``filter_allowed`` (bulk) or ``is_suppressed``
(single) before dispatch. Transactional senders must NOT — see the model
docstring for why.
"""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy.orm import Session

from app.models.email_suppression import EmailSuppression


def _normalize(email: str) -> str:
    return email.strip().lower()


class CRUDEmailSuppression:
    """Suppression rows (no commit; caller owns the txn)."""

    def is_suppressed(self, db: Session, *, email: str) -> bool:
        """True when this address has opted out of marketing email."""
        return (
            db.query(EmailSuppression.id)
            .filter(EmailSuppression.email == _normalize(email))
            .first()
            is not None
        )

    def suppress(self, db: Session, *, email: str, reason: str = "unsubscribe_link") -> None:
        """Record an opt-out. Idempotent — re-suppressing keeps the first row.

        Keeping the original row (rather than refreshing it) preserves the
        earliest opt-out timestamp, which is the one that matters if the opt-out
        is ever challenged.
        """
        normalized = _normalize(email)
        existing = (
            db.query(EmailSuppression).filter(EmailSuppression.email == normalized).one_or_none()
        )
        if existing is not None:
            return
        db.add(EmailSuppression(email=normalized, reason=reason))

    def unsuppress(self, db: Session, *, email: str) -> None:
        """Remove an opt-out (only ever on a fresh, explicit re-opt-in)."""
        db.query(EmailSuppression).filter(EmailSuppression.email == _normalize(email)).delete(
            synchronize_session=False
        )

    def suppressed_set(self, db: Session, *, emails: Iterable[str]) -> set[str]:
        """The normalised subset of ``emails`` that is suppressed.

        One query for the whole audience rather than N — a campaign resolves
        thousands of addresses and must not issue a lookup per recipient.
        """
        normalized = {_normalize(e) for e in emails if e}
        if not normalized:
            return set()
        rows = db.query(EmailSuppression.email).filter(EmailSuppression.email.in_(normalized)).all()
        return {email for (email,) in rows}

    def filter_allowed(self, db: Session, *, emails: Iterable[str]) -> list[str]:
        """``emails`` minus the suppressed ones, order and casing preserved."""
        candidates = list(emails)
        blocked = self.suppressed_set(db, emails=candidates)
        return [e for e in candidates if _normalize(e) not in blocked]


email_suppression = CRUDEmailSuppression()
