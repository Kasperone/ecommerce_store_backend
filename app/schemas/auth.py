from pydantic import Field
from typing import Optional
from app.schemas.base import BaseSchema


class Token(BaseSchema):
    """Schema for JWT token response"""
    
    access_token: str
    token_type: str = "bearer"


class TokenData(BaseSchema):
    """Schema for token payload data"""
    
    user_id: int
    email: str
    role: str


class PasswordChange(BaseSchema):
    """Schema for password change"""
    
    old_password: str
    new_password: str = Field(..., min_length=8, max_length=100)


class PasswordReset(BaseSchema):
    """Schema for password reset request"""
    
    email: str


class PasswordResetConfirm(BaseSchema):
    """Schema for password reset confirmation"""
    
    token: str
    new_password: str = Field(..., min_length=8, max_length=100)


class EmailVerification(BaseSchema):
    """Schema for email verification"""
    
    token: str


class EmailResend(BaseSchema):
    """Schema for resending verification email"""
    
    email: str
