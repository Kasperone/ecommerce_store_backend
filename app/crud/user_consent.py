"""CRUD for the append-only consent audit log (`user_consents`).

Consent is never updated in place: every grant/withdrawal INSERTs a new row
(D-B-5 / D-ACCT-3). The "current" snapshot is the latest row per
``(user_id, consent_type)``. This module owns both the append and the
latest-per-type read used by `GET /me/preferences` + the data export.
"""

from collections.abc import Collection

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.user_consent import UserConsent


class CRUDUserConsent:
    """Append + latest-per-type read for marketing/consent records."""

    def record(
        self,
        db: Session,
        *,
        user_id: int,
        consent_type: str,
        granted: bool,
        source: str | None = None,
        ip: str | None = None,
        user_agent: str | None = None,
    ) -> UserConsent:
        """Append a new immutable consent event. Caller commits."""
        obj = UserConsent(
            user_id=user_id,
            consent_type=consent_type,
            granted=granted,
            source=source,
            ip=ip,
            user_agent=(user_agent or None),
        )
        db.add(obj)
        return obj

    def list_for_user(self, db: Session, *, user_id: int) -> list[UserConsent]:
        """All consent rows for a user, newest-first (full audit trail)."""
        return (
            db.query(UserConsent)
            .filter(UserConsent.user_id == user_id)
            .order_by(UserConsent.created_at.desc(), UserConsent.id.desc())
            .all()
        )

    def granted_user_ids(
        self,
        db: Session,
        *,
        user_ids: Collection[int],
        consent_types: Collection[str],
    ) -> set[int]:
        """Of ``user_ids``, those whose LATEST row for ANY of ``consent_types``
        is ``granted``.

        The bulk counterpart to :meth:`current_map`, which answers the same
        question for one user by walking the whole audit trail in Python. A
        marketing audience can be tens of thousands of users, so per-user
        iteration would issue one query each — this is a single query.

        "Latest" uses the same ordering as :meth:`current_map`
        (``created_at`` then ``id``, both descending) so the two can never
        disagree about which row wins. Users with no row for any of the types
        are simply absent from the result: never-touched is not consent.
        """
        if not user_ids or not consent_types:
            return set()

        ranked = (
            select(
                UserConsent.user_id,
                UserConsent.granted,
                func.row_number()
                .over(
                    partition_by=(UserConsent.user_id, UserConsent.consent_type),
                    order_by=(UserConsent.created_at.desc(), UserConsent.id.desc()),
                )
                .label("rn"),
            )
            .where(
                UserConsent.user_id.in_(user_ids),
                UserConsent.consent_type.in_(consent_types),
            )
            .subquery()
        )
        rows = db.execute(
            select(ranked.c.user_id).where(ranked.c.rn == 1, ranked.c.granted.is_(True)).distinct()
        ).all()
        return {user_id for (user_id,) in rows}

    def current_map(self, db: Session, *, user_id: int) -> dict[str, bool]:
        """Latest ``granted`` value per ``consent_type`` for the user.

        Returns ``{consent_type: granted}``. Types the user has never touched are
        simply absent (the API maps absent → false/unset). Computed in Python over
        the newest-first list so the first occurrence of each type wins — keeps the
        rule identical to the audit-trail ordering and avoids a window-function
        dependency.
        """
        result: dict[str, bool] = {}
        for row in self.list_for_user(db, user_id=user_id):
            if row.consent_type not in result:
                result[row.consent_type] = row.granted
        return result


user_consent = CRUDUserConsent()
