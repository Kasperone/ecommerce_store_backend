import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import BaseModel

if TYPE_CHECKING:
    from app.models.user import User


class VerificationToken(BaseModel):
    """Email verification token model"""

    __tablename__ = "verification_tokens"

    # Token fields
    token: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id", ondelete="CASCADE"))
    expires_at: Mapped[datetime] = mapped_column(DateTime)

    # Relationships
    user: Mapped["User"] = relationship(
        "User", back_populates="verification_tokens", passive_deletes=True
    )

    def __repr__(self):
        return f"<VerificationToken {self.token[:8]}... for user_id={self.user_id}>"

    @property
    def is_expired(self) -> bool:
        """Check if token has expired"""
        now = datetime.now(UTC)
        expires_at = self.expires_at
        if expires_at.tzinfo is None:
            # Existing rows were stored as naive UTC; compare like-for-like.
            now = now.replace(tzinfo=None)
        return now > expires_at

    @staticmethod
    def generate_token() -> str:
        """Generate a unique verification token"""
        return str(uuid.uuid4())

    @staticmethod
    def get_expiration_time(hours: int = 24) -> datetime:
        """Get expiration datetime (default 24 hours from now)"""
        return datetime.now(UTC) + timedelta(hours=hours)
