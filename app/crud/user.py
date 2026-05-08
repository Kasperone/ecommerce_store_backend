import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

from sqlalchemy.orm import Session

from app.crud.base import CRUDBase
from app.models.user import User
from app.schemas.user import UserCreate, UserUpdate
from app.core.security import get_password_hash, verify_password


PASSWORD_RESET_TOKEN_TTL = timedelta(hours=1)


def _hash_reset_token(token: str) -> str:
    """SHA-256 hex digest of an opaque reset token. Stored in DB; the plain
    token only ever appears in the email link."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _utcnow_naive() -> datetime:
    """UTC now as naive datetime, matching the model's column type."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class CRUDUser(CRUDBase[User, UserCreate, UserUpdate]):
    """CRUD operations for User model"""
    
    def get_by_email(self, db: Session, *, email: str) -> Optional[User]:
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
    
    def authenticate(
        self, db: Session, *, email: str, password: str
    ) -> Optional[User]:
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
    ) -> Tuple[bool, Optional[User]]:
        """Validate a reset token, set the new password, clear the token,
        and bump password_changed_at. Returns ``(ok, user)``.
        Generic failure on bad/expired token to avoid token enumeration."""
        token_hash = _hash_reset_token(token)
        candidate = (
            db.query(User)
            .filter(User.password_reset_token_hash == token_hash)
            .first()
        )
        if not candidate or not candidate.password_reset_expires_at:
            return False, None
        if candidate.password_reset_expires_at < _utcnow_naive():
            return False, None

        candidate.hashed_password = get_password_hash(new_password)
        candidate.password_reset_token_hash = None
        candidate.password_reset_expires_at = None
        candidate.password_changed_at = _utcnow_naive()
        db.add(candidate)
        db.commit()
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
        user.password_changed_at = _utcnow_naive()
        # Belt-and-braces: invalidate any pending reset link.
        user.password_reset_token_hash = None
        user.password_reset_expires_at = None
        db.add(user)
        db.commit()
        db.refresh(user)
        return True


user = CRUDUser(User)
