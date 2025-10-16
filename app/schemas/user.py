from pydantic import EmailStr, Field, field_validator
from typing import Optional
from app.schemas.base import BaseSchema, TimestampSchema
from app.models.user import UserRole


class UserBase(BaseSchema):
    """Base user schema with common fields"""
    
    email: EmailStr
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    phone: Optional[str] = None


class UserCreate(UserBase):
    """Schema for user registration"""
    
    password: str = Field(..., min_length=8, max_length=100)
    
    @field_validator("password")
    @classmethod
    def validate_password(cls, v: str) -> str:
        """Validate password strength"""
        if not any(char.isdigit() for char in v):
            raise ValueError("Password must contain at least one digit")
        if not any(char.isupper() for char in v):
            raise ValueError("Password must contain at least one uppercase letter")
        return v


class UserUpdate(BaseSchema):
    """Schema for updating user profile"""
    
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    phone: Optional[str] = None


class UserResponse(UserBase, TimestampSchema):
    """Schema for user response (public data)"""
    
    is_active: bool
    is_verified: bool
    role: UserRole


class UserInDB(UserResponse):
    """Schema for user in database (includes password hash)"""
    
    hashed_password: str


class UserLogin(BaseSchema):
    """Schema for user login"""
    
    email: EmailStr
    password: str
