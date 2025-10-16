from pydantic import Field, field_validator
from typing import Optional, List
from app.schemas.base import BaseSchema, TimestampSchema
from app.schemas.category import CategoryResponse


class ProductBase(BaseSchema):
    """Base product schema"""
    
    name: str = Field(..., min_length=1, max_length=200)
    slug: str = Field(..., min_length=1, max_length=200)
    description: Optional[str] = None
    sku: Optional[str] = Field(None, max_length=100)
    
    # Multi-currency prices
    price_usd: float = Field(..., gt=0)
    price_pln: float = Field(..., gt=0)
    price_eur: float = Field(..., gt=0)
    
    stock: int = Field(default=0, ge=0)
    is_active: bool = True
    is_featured: bool = False
    
    images: List[str] = Field(default_factory=list)
    category_id: Optional[int] = None
    
    @field_validator("images")
    @classmethod
    def validate_images(cls, v: List[str]) -> List[str]:
        """Validate image URLs"""
        if len(v) > 10:
            raise ValueError("Maximum 10 images allowed")
        return v


class ProductCreate(ProductBase):
    """Schema for creating a product"""
    pass


class ProductUpdate(BaseSchema):
    """Schema for updating a product"""
    
    name: Optional[str] = Field(None, min_length=1, max_length=200)
    slug: Optional[str] = Field(None, min_length=1, max_length=200)
    description: Optional[str] = None
    sku: Optional[str] = Field(None, max_length=100)
    
    price_usd: Optional[float] = Field(None, gt=0)
    price_pln: Optional[float] = Field(None, gt=0)
    price_eur: Optional[float] = Field(None, gt=0)
    
    stock: Optional[int] = Field(None, ge=0)
    is_active: Optional[bool] = None
    is_featured: Optional[bool] = None
    
    images: Optional[List[str]] = None
    category_id: Optional[int] = None


class ProductResponse(ProductBase, TimestampSchema):
    """Schema for product response"""
    
    in_stock: bool
    
    @property
    def in_stock(self) -> bool:
        """Check if product is in stock"""
        return self.stock > 0


class ProductWithCategory(ProductResponse):
    """Product response with category details"""
    
    category: Optional[CategoryResponse] = None


class ProductListResponse(BaseSchema):
    """Schema for paginated product list"""
    
    items: List[ProductResponse]
    total: int
    page: int
    page_size: int
    pages: int
