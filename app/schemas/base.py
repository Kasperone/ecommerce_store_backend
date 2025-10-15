from pydantic import BaseModel, ConfigDict
from datetime import datetime


class BaseSchema(BaseModel):
    """Base schema with common configuration"""
    
    model_config = ConfigDict(
        from_attributes=True,  # Allows creating from ORM models
        populate_by_name=True,
        use_enum_values=True,
    )


class TimestampSchema(BaseSchema):
    """Schema with timestamp fields"""
    
    id: int
    created_at: datetime
    updated_at: datetime
