import enum
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, Integer, String
from sqlalchemy import Enum as SQLEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import BaseModel, _utcnow

if TYPE_CHECKING:
    from app.models.customer_note import CustomerNote
    from app.models.order import Order
    from app.models.refresh_token import RefreshToken
    from app.models.review import Review
    from app.models.tag import Tag
    from app.models.verification_token import VerificationToken


class UserRole(str, enum.Enum):
    """User roles"""

    CUSTOMER = "CUSTOMER"
    ADMIN = "ADMIN"


class User(BaseModel):
    """User model for authentication and profile"""

    __tablename__ = "users"

    # Authentication
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    hashed_password: Mapped[str] = mapped_column(String(255))  # Argon2id hashed
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_verified: Mapped[bool] = mapped_column(Boolean, default=False)

    # Profile
    first_name: Mapped[str] = mapped_column(String(100))
    last_name: Mapped[str] = mapped_column(String(100))
    phone: Mapped[str | None] = mapped_column(String(20))

    # Shipping Address (optional)
    shipping_street: Mapped[str | None] = mapped_column(String(255))
    shipping_city: Mapped[str | None] = mapped_column(String(100))
    shipping_postal_code: Mapped[str | None] = mapped_column(String(20))
    shipping_country: Mapped[str | None] = mapped_column(String(100))
    shipping_state: Mapped[str | None] = mapped_column(String(100))

    # Company/Invoice Data (optional)
    company_name: Mapped[str | None] = mapped_column(String(255))
    company_tax_id: Mapped[str | None] = mapped_column(String(50))
    company_address_street: Mapped[str | None] = mapped_column(String(255))
    company_address_city: Mapped[str | None] = mapped_column(String(100))
    company_address_postal_code: Mapped[str | None] = mapped_column(String(20))
    company_address_country: Mapped[str | None] = mapped_column(String(100))
    company_address_state: Mapped[str | None] = mapped_column(String(100))

    # Password reset / lifecycle
    # SHA-256 hash of an opaque reset token; the plain token only lives in the
    # email link. Cleared on confirm or expiry.
    password_reset_token_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    password_reset_expires_at: Mapped[datetime | None] = mapped_column(DateTime)
    # Bumped on every successful password change/reset. Used (post Phase 5) to
    # invalidate access tokens issued before the change.
    password_changed_at: Mapped[datetime | None] = mapped_column(DateTime, default=_utcnow)

    # Brute-force lockout. Counter increments on every wrong password; once it
    # crosses the threshold, ``locked_until`` is set and login is rejected with
    # the same generic 401 as a bad password (don't reveal lockout state).
    failed_login_attempts: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime)

    # Role
    role: Mapped[UserRole] = mapped_column(SQLEnum(UserRole), default=UserRole.CUSTOMER)

    # Relationships
    orders: Mapped[list["Order"]] = relationship(
        "Order", back_populates="user", cascade="all, delete-orphan"
    )
    verification_tokens: Mapped[list["VerificationToken"]] = relationship(
        "VerificationToken", back_populates="user", cascade="all, delete-orphan"
    )
    refresh_tokens: Mapped[list["RefreshToken"]] = relationship(
        "RefreshToken", back_populates="user", cascade="all, delete-orphan"
    )
    reviews: Mapped[list["Review"]] = relationship(
        "Review", back_populates="user", cascade="all, delete-orphan"
    )
    # Notes written *about* this user by admins. ``CustomerNote`` has two FKs to
    # users (subject + author); name the subject FK so SQLAlchemy can resolve it.
    notes: Mapped[list["CustomerNote"]] = relationship(
        "CustomerNote",
        foreign_keys="CustomerNote.user_id",
        back_populates="user",
        cascade="all, delete-orphan",
    )
    # Reusable admin labels attached to this customer (many-to-many).
    tags: Mapped[list["Tag"]] = relationship(
        "Tag", secondary="customer_tags", back_populates="customers"
    )

    def __repr__(self):
        return f"<User {self.email}>"

    @property
    def full_name(self):
        """Get user's full name"""
        if self.first_name and self.last_name:
            return f"{self.first_name} {self.last_name}"
        return self.email

    @property
    def is_locked(self) -> bool:
        """Whether the user is currently locked out from login (naive-UTC compare)."""
        if not self.locked_until:
            return False
        return self.locked_until > datetime.now(UTC).replace(tzinfo=None)
