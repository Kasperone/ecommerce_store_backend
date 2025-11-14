from sqlalchemy import Column, String, Boolean, Enum as SQLEnum
from sqlalchemy.orm import relationship
import enum
from app.models.base import BaseModel


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
    
    # Role
    role = Column(SQLEnum(UserRole), default=UserRole.CUSTOMER, nullable=False)
    
    # Relationships
    orders = relationship("Order", back_populates="user", cascade="all, delete-orphan")
    
    def __repr__(self):
        return f"<User {self.email}>"
    
    @property
    def full_name(self):
        """Get user's full name"""
        if self.first_name and self.last_name:
            return f"{self.first_name} {self.last_name}"
        return self.email
