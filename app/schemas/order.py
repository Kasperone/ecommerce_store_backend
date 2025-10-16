from pydantic import Field
from typing import Optional, List
from app.schemas.base import BaseSchema, TimestampSchema
from app.schemas.user import UserResponse
from app.models.order import OrderStatus


class OrderItemBase(BaseSchema):
    """Base order item schema"""
    
    product_id: int
    quantity: int = Field(..., gt=0)


class OrderItemCreate(OrderItemBase):
    """Schema for creating an order item"""
    pass


class OrderItemResponse(OrderItemBase, TimestampSchema):
    """Schema for order item response"""
    
    order_id: int
    product_name: str
    product_sku: Optional[str]
    unit_price: float
    total_price: float


class OrderBase(BaseSchema):
    """Base order schema"""
    
    # Shipping address
    shipping_address: str = Field(..., min_length=1)
    shipping_city: str = Field(..., min_length=1, max_length=100)
    shipping_postal_code: str = Field(..., min_length=1, max_length=20)
    shipping_country: str = Field(..., min_length=1, max_length=100)
    
    # Optional fields
    notes: Optional[str] = None


class OrderCreate(OrderBase):
    """Schema for creating an order"""
    
    items: List[OrderItemCreate] = Field(..., min_length=1)
    currency: str = Field(default="USD", pattern="^(USD|PLN|EUR)$")


class OrderUpdate(BaseSchema):
    """Schema for updating an order (admin only)"""
    
    status: Optional[OrderStatus] = None
    payment_method: Optional[str] = None
    payment_intent_id: Optional[str] = None
    notes: Optional[str] = None


class OrderResponse(OrderBase, TimestampSchema):
    """Schema for order response"""
    
    user_id: int
    order_number: str
    status: OrderStatus
    
    # Pricing
    subtotal: float
    tax: float
    shipping: float
    total: float
    currency: str
    
    # Payment
    payment_method: Optional[str]
    payment_intent_id: Optional[str]
    
    # Items
    items: List[OrderItemResponse] = []


class OrderWithUser(OrderResponse):
    """Order response with user details"""
    
    user: UserResponse


class OrderListResponse(BaseSchema):
    """Schema for paginated order list"""
    
    items: List[OrderResponse]
    total: int
    page: int
    page_size: int
    pages: int
