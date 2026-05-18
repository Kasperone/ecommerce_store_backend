import enum
from typing import TYPE_CHECKING

from sqlalchemy import Enum as SQLEnum
from sqlalchemy import Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import BaseModel

if TYPE_CHECKING:
    from app.models.product import Product
    from app.models.user import User


class OrderStatus(str, enum.Enum):
    """Order status enum"""

    PENDING = "pending"
    PAID = "paid"
    PROCESSING = "processing"
    SHIPPED = "shipped"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"
    REFUNDED = "refunded"


class Order(BaseModel):
    """Order model"""

    __tablename__ = "orders"

    # User
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"))

    # Order details
    order_number: Mapped[str] = mapped_column(String(50), unique=True, index=True)
    status: Mapped[OrderStatus] = mapped_column(SQLEnum(OrderStatus), default=OrderStatus.PENDING)

    # Pricing
    subtotal: Mapped[float] = mapped_column(Float)
    tax: Mapped[float] = mapped_column(Float, default=0)
    shipping: Mapped[float] = mapped_column(Float, default=0)
    total: Mapped[float] = mapped_column(Float)
    currency: Mapped[str] = mapped_column(String(3), default="USD")  # USD, PLN, EUR

    # Shipping address
    shipping_address: Mapped[str] = mapped_column(Text)
    shipping_city: Mapped[str] = mapped_column(String(100))
    shipping_postal_code: Mapped[str] = mapped_column(String(20))
    shipping_country: Mapped[str] = mapped_column(String(100))

    # Payment
    payment_method: Mapped[str | None] = mapped_column(String(50))  # stripe, payu, etc
    payment_intent_id: Mapped[str | None] = mapped_column(String(255))  # From payment provider

    # Notes
    notes: Mapped[str | None] = mapped_column(Text)

    # Relationships
    user: Mapped["User"] = relationship("User", back_populates="orders")
    items: Mapped[list["OrderItem"]] = relationship(
        "OrderItem", back_populates="order", cascade="all, delete-orphan"
    )

    def __repr__(self):
        return f"<Order {self.order_number}>"


class OrderItem(BaseModel):
    """Order item model (products in an order)"""

    __tablename__ = "order_items"

    # Order
    order_id: Mapped[int] = mapped_column(Integer, ForeignKey("orders.id"))

    # Product
    product_id: Mapped[int] = mapped_column(Integer, ForeignKey("products.id"))

    # Item details (snapshot at time of order)
    product_name: Mapped[str] = mapped_column(String(200))
    product_sku: Mapped[str | None] = mapped_column(String(100))
    quantity: Mapped[int] = mapped_column(Integer)
    unit_price: Mapped[float] = mapped_column(Float)
    total_price: Mapped[float] = mapped_column(Float)

    # Relationships
    order: Mapped["Order"] = relationship("Order", back_populates="items")
    product: Mapped["Product"] = relationship("Product", back_populates="order_items")

    def __repr__(self):
        return f"<OrderItem {self.product_name} x{self.quantity}>"
