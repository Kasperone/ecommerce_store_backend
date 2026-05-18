from pydantic import Field, computed_field, field_validator

from app.schemas.base import BaseSchema, TimestampSchema
from app.schemas.category import CategoryResponse


class ProductBase(BaseSchema):
    """Base product schema"""

    name: str = Field(..., min_length=1, max_length=200)
    slug: str = Field(..., min_length=1, max_length=200)
    description: str | None = None
    sku: str | None = Field(None, max_length=100)

    # Multi-currency prices
    price_usd: float = Field(..., gt=0)
    price_pln: float = Field(..., gt=0)
    price_eur: float = Field(..., gt=0)

    stock: int = Field(default=0, ge=0)
    is_active: bool = True
    is_featured: bool = False

    images: list[str] = Field(default_factory=list)
    category_id: int | None = None

    @field_validator("images")
    @classmethod
    def validate_images(cls, v: list[str]) -> list[str]:
        """Validate image URLs"""
        if len(v) > 10:
            raise ValueError("Maximum 10 images allowed")
        return v


class ProductCreate(ProductBase):
    """Schema for creating a product"""

    pass


class ProductUpdate(BaseSchema):
    """Schema for updating a product"""

    name: str | None = Field(None, min_length=1, max_length=200)
    slug: str | None = Field(None, min_length=1, max_length=200)
    description: str | None = None
    sku: str | None = Field(None, max_length=100)

    price_usd: float | None = Field(None, gt=0)
    price_pln: float | None = Field(None, gt=0)
    price_eur: float | None = Field(None, gt=0)

    stock: int | None = Field(None, ge=0)
    is_active: bool | None = None
    is_featured: bool | None = None

    images: list[str] | None = None
    category_id: int | None = None


class ProductResponse(ProductBase, TimestampSchema):
    """Schema for product response"""

    @computed_field  # type: ignore[prop-decorator]
    @property
    def in_stock(self) -> bool:
        """True when stock > 0."""
        return self.stock > 0


class ProductWithCategory(ProductResponse):
    """Product response with category details"""

    category: CategoryResponse | None = None


class ProductListResponse(BaseSchema):
    """Schema for paginated product list"""

    items: list[ProductResponse]
    total: int
    page: int
    page_size: int
    pages: int
