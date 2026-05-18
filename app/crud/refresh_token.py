"""CRUD for the rotating refresh-token state.

The DB row is the source of truth for whether a refresh JWT is still usable.
A signed token whose row is missing, revoked, or expired is rejected; this
is what makes server-side logout possible.

Reuse detection: if a presented refresh JWT validates cryptographically but
its row is already revoked, that's prima-facie token theft (the legitimate
client has already rotated past it). The whole family for that user is
revoked and the incident is logged for the operator.
"""

from datetime import UTC, datetime
from hmac import compare_digest

from sqlalchemy.orm import Session

from app.core.security import hash_refresh_token
from app.models.refresh_token import RefreshToken


def _utcnow_naive() -> datetime:
    """Match the column type (naive UTC) used elsewhere in the project."""
    return datetime.now(UTC).replace(tzinfo=None)


class CRUDRefreshToken:
    """Persistence + lifecycle for refresh tokens."""

    def create(
        self,
        db: Session,
        *,
        user_id: int,
        jti: str,
        token: str,
        expires_at: datetime,
        ip: str | None = None,
        user_agent: str | None = None,
    ) -> RefreshToken:
        # Strip tz to match the naive-UTC convention used by the rest of the app.
        if expires_at.tzinfo is not None:
            expires_at = expires_at.astimezone(UTC).replace(tzinfo=None)
        obj = RefreshToken(
            user_id=user_id,
            jti=jti,
            token_hash=hash_refresh_token(token),
            expires_at=expires_at,
            created_ip=ip,
            user_agent=(user_agent or None),
        )
        db.add(obj)
        db.commit()
        db.refresh(obj)
        return obj

    def get_by_jti(self, db: Session, *, jti: str) -> RefreshToken | None:
        return db.query(RefreshToken).filter(RefreshToken.jti == jti).first()

    def is_revoked(self, row: RefreshToken) -> bool:
        return row.revoked_at is not None

    def is_expired(self, row: RefreshToken) -> bool:
        return row.expires_at <= _utcnow_naive()

    def matches(self, row: RefreshToken, *, token: str) -> bool:
        """Constant-time compare against the stored hash."""
        return compare_digest(row.token_hash, hash_refresh_token(token))  # pyright: ignore[reportCallIssue] -- row.token_hash typed as Column[str] until Phase 3 Mapped[str] migration

    def revoke(
        self,
        db: Session,
        *,
        row: RefreshToken,
        replaced_by_jti: str | None = None,
    ) -> RefreshToken:
        if row.revoked_at is None:
            row.revoked_at = _utcnow_naive()
        if replaced_by_jti is not None:
            row.replaced_by_jti = replaced_by_jti
        db.add(row)
        db.commit()
        db.refresh(row)
        return row

    def revoke_all_for_user(self, db: Session, *, user_id: int) -> int:
        """Revoke every active refresh row for ``user_id``. Returns count.

        Used by /logout-all and by reuse-detection to nuke a token family.
        """
        now = _utcnow_naive()
        rows = (
            db.query(RefreshToken)
            .filter(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
            .all()
        )
        for r in rows:
            r.revoked_at = now
            db.add(r)
        db.commit()
        return len(rows)

    def rotate(
        self,
        db: Session,
        *,
        old: RefreshToken,
        new_jti: str,
        new_token: str,
        new_expires_at: datetime,
        ip: str | None = None,
        user_agent: str | None = None,
    ) -> tuple[RefreshToken, RefreshToken]:
        """Atomically revoke ``old`` and insert the replacement row.

        Returns ``(old_revoked, new_row)``. Both writes happen in a single
        commit so a crash mid-rotation can't leave the family in a state
        where reuse-detection would mistake a partial rotation for theft.
        """
        if new_expires_at.tzinfo is not None:
            new_expires_at = new_expires_at.astimezone(UTC).replace(tzinfo=None)
        new_row = RefreshToken(
            user_id=old.user_id,
            jti=new_jti,
            token_hash=hash_refresh_token(new_token),
            expires_at=new_expires_at,
            created_ip=ip,
            user_agent=(user_agent or None),
        )
        old.revoked_at = _utcnow_naive()
        old.replaced_by_jti = new_jti
        db.add(old)
        db.add(new_row)
        db.commit()
        db.refresh(old)
        db.refresh(new_row)
        return old, new_row


refresh_token = CRUDRefreshToken()
