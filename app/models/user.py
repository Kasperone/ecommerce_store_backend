from sqlalchemy import Column, String, Boolean, DateTime, Integer, Enum as SQLEnum
from sqlalchemy.orm import relationship
import enum
from app.models.base import BaseModel, _utcnow


class UserRole(str, enum.Enum):
    """User roles"""
    CUSTOMER = "CUSTOMER"
    ADMIN = "ADMIN"


class User(BaseModel):
    """User model for authentication and profile"""
    
    __tablename__ = "users"
    
    # Authentication
    email = Column(String(255), unique=True, index=True, nullable=False)
    hashed_password = Column(String(255), nullable=False)  # Argon2id hashed
    is_active = Column(Boolean, default=True, nullable=False)
    is_verified = Column(Boolean, default=False, nullable=False)
    
    # Profile
    first_name = Column(String(100), nullable=False)
    last_name = Column(String(100), nullable=False)
    phone = Column(String(20), nullable=True)
    
    # Shipping Address (optional)
    shipping_street = Column(String(255), nullable=True)
    shipping_city = Column(String(100), nullable=True)
    shipping_postal_code = Column(String(20), nullable=True)
    shipping_country = Column(String(100), nullable=True)
    shipping_state = Column(String(100), nullable=True)
    
    # Company/Invoice Data (optional)
    company_name = Column(String(255), nullable=True)
    company_tax_id = Column(String(50), nullable=True)
    company_address_street = Column(String(255), nullable=True)
    company_address_city = Column(String(100), nullable=True)
    company_address_postal_code = Column(String(20), nullable=True)
    company_address_country = Column(String(100), nullable=True)
    company_address_state = Column(String(100), nullable=True)
    
    # Password reset / lifecycle
    # SHA-256 hash of an opaque reset token; the plain token only lives in the
    # email link. Cleared on confirm or expiry.
    password_reset_token_hash = Column(String(64), nullable=True, index=True)
    password_reset_expires_at = Column(DateTime, nullable=True)
    # Bumped on every successful password change/reset. Used (post Phase 5) to
    # invalidate access tokens issued before the change.
    password_changed_at = Column(DateTime, default=_utcnow, nullable=True)

    # Brute-force lockout. Counter increments on every wrong password; once it
    # crosses the threshold, ``locked_until`` is set and login is rejected with
    # the same generic 401 as a bad password (don't reveal lockout state).
    failed_login_attempts = Column(Integer, default=0, nullable=False)
    locked_until = Column(DateTime, nullable=True)

    # Role
    role = Column(SQLEnum(UserRole), default=UserRole.CUSTOMER, nullable=False)
    
    # Relationships
    orders = relationship("Order", back_populates="user", cascade="all, delete-orphan")
    verification_tokens = relationship("VerificationToken", back_populates="user", cascade="all, delete-orphan")
    refresh_tokens = relationship("RefreshToken", back_populates="user", cascade="all, delete-orphan")
    
    def __repr__(self):
        return f"<User {self.email}>"
    
    @property
    def full_name(self):
        """Get user's full name"""
        if self.first_name and self.last_name:
            return f"{self.first_name} {self.last_name}"
        return self.email
