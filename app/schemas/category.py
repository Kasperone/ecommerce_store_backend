from pydantic import Field

from app.schemas.base import BaseSchema, TimestampSchema


class CategoryBase(BaseSchema):
    """Base category schema"""

    name: str = Field(..., min_length=1, max_length=100)
    slug: str = Field(..., min_length=1, max_length=100)
    description: str | None = None
    is_active: bool = True


class CategoryCreate(CategoryBase):
    """Schema for creating a category"""

    pass


class CategoryUpdate(BaseSchema):
    """Schema for updating a category"""

    name: str | None = Field(None, min_length=1, max_length=100)
    slug: str | None = Field(None, min_length=1, max_length=100)
    description: str | None = None
    is_active: bool | None = None


class CategoryResponse(CategoryBase, TimestampSchema):
    """Schema for category response"""

    pass


class CategoryWithProductCount(CategoryResponse):
    """Category response with product count"""

    product_count: int = 0
