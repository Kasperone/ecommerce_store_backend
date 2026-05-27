import hashlib
import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, or_
from sqlalchemy.orm import Session, selectinload

from app.core.security import get_password_hash, verify_password
from app.crud.base import CRUDBase
from app.crud.refresh_token import refresh_token as crud_refresh_token
from app.models.order import Order, OrderStatus
from app.models.tag import Tag
from app.models.user import User, UserRole
from app.schemas.user import UserCreate, UserUpdate

PASSWORD_RESET_TOKEN_TTL = timedelta(hours=1)

# Account lockout policy: after this many consecutive wrong passwords, the
# account is locked for ``LOGIN_LOCKOUT_DURATION``. Login then returns the
# same generic 401 as a bad password — the lockout state is intentionally
# not surfaced so attackers can't probe it.
LOGIN_LOCKOUT_THRESHOLD = 10
LOGIN_LOCKOUT_DURATION = timedelta(minutes=30)


def _hash_reset_token(token: str) -> str:
    """SHA-256 hex digest of an opaque reset token. Stored in DB; the plain
    token only ever appears in the email link."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _utcnow_naive() -> datetime:
    """UTC now as naive datetime, matching the model's column type."""
    return datetime.now(UTC).replace(tzinfo=None)


def _password_changed_bump() -> datetime:
    """Stamp ``password_changed_at`` strictly past any in-flight access
    token's ``iat`` (whole seconds). Adding one second ensures tokens
    issued in the same whole second as the bump are still invalidated."""
    return _utcnow_naive() + timedelta(seconds=1)


class CRUDUser(CRUDBase[User, UserCreate, UserUpdate]):
    """CRUD operations for User model"""

    # Re-exposed as class attributes so callers can read the policy without
    # importing module-level constants (e.g. the login route formats the
    # duration into the lockout email).
    LOGIN_LOCKOUT_THRESHOLD = LOGIN_LOCKOUT_THRESHOLD
    LOGIN_LOCKOUT_DURATION = LOGIN_LOCKOUT_DURATION

    def get_by_email(self, db: Session, *, email: str) -> User | None:
        """
        Get user by email

        Args:
            db: Database session
            email: User email

        Returns:
            User object or None
        """
        return db.query(User).filter(User.email == email).first()

    def create(self, db: Session, *, obj_in: UserCreate) -> User:
        """
        Create new user with hashed password

        Args:
            db: Database session
            obj_in: User creation schema

        Returns:
            Created user object
        """
        db_obj = User(
            email=obj_in.email,
            hashed_password=get_password_hash(obj_in.password),
            first_name=obj_in.first_name,
            last_name=obj_in.last_name,
            phone=obj_in.phone or None,
        )
        db.add(db_obj)
        db.commit()
        db.refresh(db_obj)
        return db_obj

    def authenticate(self, db: Session, *, email: str, password: str) -> User | None:
        """
        Authenticate user with email and password

        Args:
            db: Database session
            email: User email
            password: Plain text password

        Returns:
            User object if authenticated, None otherwise
        """
        user = self.get_by_email(db, email=email)
        if not user:
            return None
        if not verify_password(password, user.hashed_password):
            return None
        return user

    def is_active(self, user: User) -> bool:
        """Check if user is active"""
        return user.is_active

    def is_admin(self, user: User) -> bool:
        """Check if user is admin"""
        from app.models.user import UserRole

        return user.role == UserRole.ADMIN

    def is_locked(self, user: User) -> bool:
        """Whether the user is currently locked out from login."""
        if not user.locked_until:
            return False
        return user.locked_until > _utcnow_naive()

    def register_failed_login(self, db: Session, *, user: User) -> bool:
        """Bump the failed-login counter; lock the account once the threshold
        is crossed. Safe to call on every wrong password.

        Returns ``True`` if this attempt caused the account to be (re)locked
        — the caller uses that signal to send the out-of-band lockout email.
        The route only invokes this when the account is **not** currently
        locked (locked accounts short-circuit earlier in the login flow), so
        a True return reliably means "transitioned from unlocked to locked".
        """
        user.failed_login_attempts = (user.failed_login_attempts or 0) + 1
        just_locked = user.failed_login_attempts >= LOGIN_LOCKOUT_THRESHOLD
        if just_locked:
            user.locked_until = _utcnow_naive() + LOGIN_LOCKOUT_DURATION
        db.add(user)
        db.commit()
        db.refresh(user)
        return just_locked

    def register_successful_login(self, db: Session, *, user: User) -> None:
        """Clear failed-login state on a successful authentication."""
        if user.failed_login_attempts or user.locked_until:
            user.failed_login_attempts = 0
            user.locked_until = None
            db.add(user)
            db.commit()
            db.refresh(user)

    def _clear_lockout_in_place(self, user: User) -> None:
        """Reset lockout fields on ``user`` without committing. Used by the
        password reset/change flows so a successful rotation also re-enables
        login."""
        user.failed_login_attempts = 0
        user.locked_until = None

    def issue_password_reset_token(self, db: Session, *, user: User) -> str:
        """Generate a one-time password reset token and persist its SHA-256
        hash + 1h expiry on the user. Returns the plain token (only delivered
        via email)."""
        token = secrets.token_urlsafe(32)
        user.password_reset_token_hash = _hash_reset_token(token)
        user.password_reset_expires_at = _utcnow_naive() + PASSWORD_RESET_TOKEN_TTL
        db.add(user)
        db.commit()
        db.refresh(user)
        return token

    def consume_password_reset_token(
        self, db: Session, *, token: str, new_password: str
    ) -> tuple[bool, User | None]:
        """Validate a reset token, set the new password, clear the token,
        and bump password_changed_at. Returns ``(ok, user)``.
        Generic failure on bad/expired token to avoid token enumeration."""
        token_hash = _hash_reset_token(token)
        candidate = db.query(User).filter(User.password_reset_token_hash == token_hash).first()
        if not candidate or not candidate.password_reset_expires_at:
            return False, None
        if candidate.password_reset_expires_at < _utcnow_naive():
            return False, None

        candidate.hashed_password = get_password_hash(new_password)
        candidate.password_reset_token_hash = None
        candidate.password_reset_expires_at = None
        candidate.password_changed_at = _password_changed_bump()
        # Successful reset is a strong signal of legitimate ownership; clear
        # any active lockout so the user can sign in immediately.
        self._clear_lockout_in_place(candidate)
        db.add(candidate)
        # revoke_all_for_user commits internally; the password rotation on
        # `candidate` flushes in the same transaction so revocation lands
        # atomically with the rotation.
        crud_refresh_token.revoke_all_for_user(db, user_id=candidate.id)
        db.refresh(candidate)
        return True, candidate

    def change_password(
        self,
        db: Session,
        *,
        user: User,
        current_password: str,
        new_password: str,
    ) -> bool:
        """Verify the current password and rotate to ``new_password``.
        Returns False on current-password mismatch."""
        if not verify_password(current_password, user.hashed_password):
            return False
        user.hashed_password = get_password_hash(new_password)
        user.password_changed_at = _password_changed_bump()
        # Belt-and-braces: invalidate any pending reset link.
        user.password_reset_token_hash = None
        user.password_reset_expires_at = None
        # Authenticated rotation also clears any stale lockout state.
        self._clear_lockout_in_place(user)
        db.add(user)
        # revoke_all_for_user commits internally; pending rotation state
        # flushes atomically with the refresh-row revocation.
        crud_refresh_token.revoke_all_for_user(db, user_id=user.id)
        db.refresh(user)
        return True

    # ------------------------------------------------------------------
    # Admin "customers" view over the users table
    # ------------------------------------------------------------------

    def _admin_filtered_query(
        self,
        db: Session,
        *,
        role: "UserRole | None",
        is_active: bool | None,
        is_verified: bool | None,
        search: str | None,
        tag_id: int | None = None,
    ):
        query = db.query(User)
        if role is not None:
            query = query.filter(User.role == role)
        if is_active is not None:
            query = query.filter(User.is_active.is_(is_active))
        if is_verified is not None:
            query = query.filter(User.is_verified.is_(is_verified))
        if tag_id is not None:
            # EXISTS subquery — no row duplication, no DISTINCT needed.
            query = query.filter(User.tags.any(Tag.id == tag_id))
        if search:
            like = f"%{search.strip()}%"
            query = query.filter(
                or_(
                    User.email.ilike(like),
                    User.first_name.ilike(like),
                    User.last_name.ilike(like),
                )
            )
        return query

    def get_multi_admin(
        self,
        db: Session,
        *,
        role: "UserRole | None" = None,
        is_active: bool | None = None,
        is_verified: bool | None = None,
        search: str | None = None,
        tag_id: int | None = None,
        skip: int = 0,
        limit: int = 20,
    ) -> list[User]:
        """Admin list of users with filters, newest first. Tags are eager-loaded
        (one IN query for the whole page) so the route renders chips without N+1."""
        query = self._admin_filtered_query(
            db,
            role=role,
            is_active=is_active,
            is_verified=is_verified,
            search=search,
            tag_id=tag_id,
        )
        return (
            query.options(selectinload(User.tags))
            .order_by(User.created_at.desc(), User.id.desc())
            .offset(skip)
            .limit(limit)
            .all()
        )

    def count_admin(
        self,
        db: Session,
        *,
        role: "UserRole | None" = None,
        is_active: bool | None = None,
        is_verified: bool | None = None,
        search: str | None = None,
        tag_id: int | None = None,
    ) -> int:
        return self._admin_filtered_query(
            db,
            role=role,
            is_active=is_active,
            is_verified=is_verified,
            search=search,
            tag_id=tag_id,
        ).count()

    def order_counts_for(self, db: Session, *, user_ids: list[int]) -> dict[int, int]:
        """Order counts keyed by user id — one grouped query (avoids N+1 on the list)."""
        if not user_ids:
            return {}
        rows = (
            db.query(Order.user_id, func.count(Order.id))
            .filter(Order.user_id.in_(user_ids))
            .group_by(Order.user_id)
            .all()
        )
        return {uid: count for uid, count in rows}

    def order_metrics(self, db: Session, *, user_id: int) -> dict:
        """Lifetime order metrics for one customer.

        ``order_count`` / ``last_order_at`` span every order; ``total_spent``
        (per currency) excludes cancelled orders.
        """
        count, last_at = (
            db.query(func.count(Order.id), func.max(Order.created_at))
            .filter(Order.user_id == user_id)
            .one()
        )
        spent_rows = (
            db.query(Order.currency, func.sum(Order.total))
            .filter(Order.user_id == user_id, Order.status != OrderStatus.CANCELLED)
            .group_by(Order.currency)
            .all()
        )
        total_spent = {currency: round(float(amount or 0.0), 2) for currency, amount in spent_rows}
        return {
            "order_count": count or 0,
            "total_spent": total_spent,
            "last_order_at": last_at,
        }

    def unlock(self, db: Session, *, user: User) -> User:
        """Clear brute-force lockout state. Caller commits."""
        self._clear_lockout_in_place(user)
        db.add(user)
        return user


user = CRUDUser(User)
