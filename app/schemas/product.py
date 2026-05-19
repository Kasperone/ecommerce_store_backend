from pydantic import Field, computed_field

from app.schemas.base import BaseSchema, TimestampSchema
from app.schemas.category import CategoryResponse


class ProductImageResponse(TimestampSchema):
    """One image attached to a product.

    Read-only on this endpoint set — image creation, mutation, and deletion
    happen via the dedicated admin upload endpoints (PR1). ``id``,
    ``created_at``, ``updated_at`` come from ``TimestampSchema``.
    """

    product_id: int
    url: str
    alt_text: str | None = None
    sort_order: int = 0
    is_primary: bool = False
    width: int
    height: int
    bytes: int
    mime: str


class ProductImageUpdate(BaseSchema):
    """Admin-side PATCH body for an existing image row.

    Image bytes are immutable — to change the file, delete and re-upload.
    Promoting to ``is_primary=True`` triggers atomic sibling demotion in
    ``CRUDProductImage.set_primary``.
    """

    alt_text: str | None = None
    sort_order: int | None = Field(None, ge=0)
    is_primary: bool | None = None


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

    category_id: int | None = None


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

    category_id: int | None = None


class ProductResponse(ProductBase, TimestampSchema):
    """Schema for product response"""

    images: list[ProductImageResponse] = Field(default_factory=list)

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
