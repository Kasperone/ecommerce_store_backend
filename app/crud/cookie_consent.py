"""CRUD for the append-only cookie-consent audit log (`cookie_consents`).

A cookie choice is never updated in place: every save/reopen INSERTs a new row
(same rule as ``user_consents``). The "current" choice is the latest row matched
on ``anon_id`` and/or ``user_id``. This module owns the append + the latest read
used by ``GET /cookie-consent``.

``necessary`` is re-coerced ``True`` here as belt-and-braces — the schema already
enforces it, but the CRUD is the last line of defense before the row lands.
"""

from typing import Any

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models.cookie_consent import CookieConsent


class CRUDCookieConsent:
    """Append + latest read for cookie-consent records."""

    def record(
        self,
        db: Session,
        *,
        anon_id: str | None,
        user_id: int | None,
        categories: dict[str, Any],
        gpc: bool = False,
        policy_version: str | None = None,
        ip: str | None = None,
        user_agent: str | None = None,
    ) -> CookieConsent:
        """Append a new immutable cookie-consent event. Caller commits.

        ``categories.necessary`` is forced ``True`` regardless of input.
        """
        coerced = {**categories, "necessary": True}
        obj = CookieConsent(
            anon_id=anon_id,
            user_id=user_id,
            categories=coerced,
            gpc=gpc,
            policy_version=policy_version,
            ip=ip,
            user_agent=(user_agent or None),
        )
        db.add(obj)
        return obj

    def current_for(
        self,
        db: Session,
        *,
        anon_id: str | None = None,
        user_id: int | None = None,
    ) -> CookieConsent | None:
        """Latest cookie-consent row for the given anon_id and/or user_id.

        Matches on either identifier (newest-first). Returns ``None`` when no
        identifier is supplied or no row exists.
        """
        clauses = []
        if user_id is not None:
            clauses.append(CookieConsent.user_id == user_id)
        if anon_id:
            clauses.append(CookieConsent.anon_id == anon_id)
        if not clauses:
            return None
        return (
            db.query(CookieConsent)
            .filter(or_(*clauses))
            .order_by(CookieConsent.created_at.desc(), CookieConsent.id.desc())
            .first()
        )


cookie_consent = CRUDCookieConsent()
