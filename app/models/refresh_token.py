from sqlalchemy import Column, DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.orm import relationship

from app.models.base import BaseModel


class RefreshToken(BaseModel):
    """Persisted refresh-token state for rotation and reuse detection.

    Each ``/auth/login`` inserts one row. ``/auth/refresh`` rotates the row:
    the presented one is marked revoked and ``replaced_by_jti`` points at the
    new row, forming a linked family per session. Re-presenting an already
    revoked token is treated as theft and revokes the entire family.
    """

    __tablename__ = "refresh_tokens"

    user_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    jti = Column(String(64), unique=True, nullable=False, index=True)
    token_hash = Column(String(64), nullable=False)
    expires_at = Column(DateTime, nullable=False)
    revoked_at = Column(DateTime, nullable=True)
    replaced_by_jti = Column(String(64), nullable=True)

    created_ip = Column(String(64), nullable=True)
    user_agent = Column(String(512), nullable=True)

    user = relationship("User", back_populates="refresh_tokens", passive_deletes=True)

    __table_args__ = (Index("ix_refresh_tokens_user_active", "user_id", "revoked_at"),)

    def __repr__(self) -> str:
        return f"<RefreshToken jti={self.jti[:8]}... user_id={self.user_id}>"
