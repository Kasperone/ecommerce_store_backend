from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import BaseModel

if TYPE_CHECKING:
    from app.models.user import User


class RefreshToken(BaseModel):
    """Persisted refresh-token state for rotation and reuse detection.

    Each ``/auth/login`` inserts one row. ``/auth/refresh`` rotates the row:
    the presented one is marked revoked and ``replaced_by_jti`` points at the
    new row, forming a linked family per session. Re-presenting an already
    revoked token is treated as theft and revokes the entire family.
    """

    __tablename__ = "refresh_tokens"

    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    jti: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    token_hash: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime)
    replaced_by_jti: Mapped[str | None] = mapped_column(String(64))

    created_ip: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(512))

    user: Mapped["User"] = relationship(
        "User", back_populates="refresh_tokens", passive_deletes=True
    )

    __table_args__ = (Index("ix_refresh_tokens_user_active", "user_id", "revoked_at"),)

    def __repr__(self) -> str:
        return f"<RefreshToken jti={self.jti[:8]}... user_id={self.user_id}>"
