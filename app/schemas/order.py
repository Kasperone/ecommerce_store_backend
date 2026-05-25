from pydantic import Field

from app.models.order import OrderStatus
from app.schemas.base import BaseSchema, TimestampSchema
from app.schemas.user import UserResponse


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
    product_sku: str | None
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
    notes: str | None = None


class OrderCreate(OrderBase):
    """Schema for creating an order"""

    items: list[OrderItemCreate] = Field(..., min_length=1)
    currency: str = Field(default="USD", pattern="^(USD|PLN|EUR)$")


class OrderUpdate(BaseSchema):
    """Schema for updating an order (admin only)"""

    status: OrderStatus | None = None
    payment_method: str | None = None
    payment_intent_id: str | None = None
    notes: str | None = None


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
    payment_method: str | None
    payment_intent_id: str | None

    # Items
    items: list[OrderItemResponse] = Field(default_factory=list)


class OrderWithUser(OrderResponse):
    """Order response with user details"""

    user: UserResponse


class OrderListItem(TimestampSchema):
    """Lightweight order row for list views (no nested line items)."""

    order_number: str
    status: OrderStatus
    subtotal: float
    total: float
    currency: str
    item_count: int
    customer_email: str | None = None


class OrderListResponse(BaseSchema):
    """Schema for paginated order list"""

    items: list[OrderListItem]
    total: int
    page: int
    page_size: int
    pages: int
