from sqlalchemy import Column, String, Integer, ForeignKey, DateTime
from sqlalchemy.orm import relationship
from datetime import datetime, timedelta, timezone
import uuid

from app.models.base import BaseModel


class VerificationToken(BaseModel):
    """Email verification token model"""
    
    __tablename__ = "verification_tokens"
    
    # Token fields
    token = Column(String(255), unique=True, index=True, nullable=False)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    expires_at = Column(DateTime, nullable=False)
    
    # Relationships
    user = relationship("User", back_populates="verification_tokens", passive_deletes=True)
    
    def __repr__(self):
        return f"<VerificationToken {self.token[:8]}... for user_id={self.user_id}>"
    
    @property
    def is_expired(self) -> bool:
        """Check if token has expired"""
        now = datetime.now(timezone.utc)
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
        return datetime.now(timezone.utc) + timedelta(hours=hours)
