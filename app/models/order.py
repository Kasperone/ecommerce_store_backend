import enum
from typing import TYPE_CHECKING

from sqlalchemy import Enum as SQLEnum
from sqlalchemy import Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import BaseModel

if TYPE_CHECKING:
    from app.models.product import Product
    from app.models.refund import Refund
    from app.models.shipping import ShippingMethod
    from app.models.user import User


class OrderStatus(str, enum.Enum):
    """Order status enum.

    ``PARTIALLY_REFUNDED`` (v2 refunds) lives between the in-fulfilment statuses
    and the terminal ``REFUNDED``: an order with ``sum(refunds) > 0 AND <
    order.total`` carries this value, and only flips to ``REFUNDED`` once the
    cumulative refund total hits ``order.total``. Stored on the PG enum type
    via an ``ALTER TYPE … ADD VALUE`` migration (Postgres-native enums can't
    cleanly remove values, so the downgrade leaves it in place).
    """

    PENDING = "pending"
    PAID = "paid"
    PROCESSING = "processing"
    SHIPPED = "shipped"
    DELIVERED = "delivered"
    PARTIALLY_REFUNDED = "partially_refunded"
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
    # Coupon discount applied to the goods subtotal (never to shipping). 0 when no
    # coupon was redeemed. ``total == round(subtotal - discount + shipping, 2)``.
    discount: Mapped[float] = mapped_column(Float, default=0, server_default="0")
    total: Mapped[float] = mapped_column(Float)
    currency: Mapped[str] = mapped_column(String(3), default="USD")  # USD, PLN, EUR

    # Redeemed coupon (nullable — most orders carry none). ``coupon_id`` SET NULL
    # on delete so removing a coupon definition never blocks/loses an order;
    # ``coupon_code`` snapshots the code at redemption time so it survives the
    # coupon row being edited or deleted (same snapshot discipline as
    # ``shipping_method_name`` and the order_item product snapshots).
    coupon_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("coupons.id", ondelete="SET NULL")
    )
    coupon_code: Mapped[str | None] = mapped_column(String(64))

    # Shipping address
    shipping_address: Mapped[str] = mapped_column(Text)
    shipping_city: Mapped[str] = mapped_column(String(100))
    shipping_postal_code: Mapped[str] = mapped_column(String(20))
    shipping_country: Mapped[str] = mapped_column(String(100))

    # Payment
    payment_method: Mapped[str | None] = mapped_column(String(50))  # stripe, payu, etc
    payment_intent_id: Mapped[str | None] = mapped_column(String(255))  # From payment provider

    # Shipping method (nullable so pre-shipping orders survive, SET NULL so deleting
    # a deprecated method preserves the order; shipping_method_name keeps the
    # snapshot of what the customer actually paid for).
    shipping_method_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("shipping_methods.id", ondelete="SET NULL")
    )
    shipping_method_name: Mapped[str | None] = mapped_column(String(80))

    # Shipment tracking (all nullable — recorded by the admin when the order
    # ships, editable afterwards; pre-shipping orders and untracked carriers
    # carry none). ``tracking_url`` is stored verbatim when the admin provides
    # one — there is deliberately no carrier→URL-template guessing table (v1).
    shipping_carrier: Mapped[str | None] = mapped_column(String(100))
    tracking_number: Mapped[str | None] = mapped_column(String(100))
    tracking_url: Mapped[str | None] = mapped_column(String(500))

    # Notes
    notes: Mapped[str | None] = mapped_column(Text)

    # Relationships
    user: Mapped["User"] = relationship("User", back_populates="orders")
    items: Mapped[list["OrderItem"]] = relationship(
        "OrderItem", back_populates="order", cascade="all, delete-orphan"
    )
    shipping_method: Mapped["ShippingMethod | None"] = relationship("ShippingMethod")
    # Refunds against this order. Cascade is deliberately NOT delete-orphan: a
    # refund is a legal/financial record and the ``ON DELETE RESTRICT`` on
    # ``refunds.order_id`` would block the order delete anyway (orders are never
    # hard-deleted in practice — cancel is a status change).
    refunds: Mapped[list["Refund"]] = relationship(
        "Refund", back_populates="order", order_by="Refund.created_at"
    )

    def __repr__(self):
        return f"<Order {self.order_number}>"

    @property
    def item_count(self) -> int:
        """Number of line items (eager-loaded in scoped queries — no N+1)."""
        return len(self.items)

    @property
    def customer_email(self) -> str | None:
        """Email of the ordering user, for admin list views."""
        return self.user.email if self.user else None


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
