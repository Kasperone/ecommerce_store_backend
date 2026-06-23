import enum
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy import Enum as SQLEnum
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

    # Every order has an owner OR a guest contact email — never neither.
    __table_args__ = (
        CheckConstraint(
            "user_id IS NOT NULL OR guest_email IS NOT NULL",
            name="ck_orders_user_or_guest_email",
        ),
    )

    # User — nullable for guest checkout (a guest order has no user row;
    # deliberately NOT a guest-user row, which would collide with the
    # email-verification login flow and "register later with the same email").
    user_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id"))

    # Guest checkout (all NULL on authed orders). ``guest_email`` is the contact
    # address (required iff ``user_id`` is NULL — see the CHECK). ``guest_token``
    # is an opaque 256-bit tracking token (``secrets.token_urlsafe(32)``),
    # unique-indexed, generated ONLY for guest orders — it backs the public
    # ``GET /orders/track/{token}`` view and the guest payment-start calls.
    # ``guest_locale`` is the email language (no ``User.locale`` to read).
    guest_email: Mapped[str | None] = mapped_column(String(255), index=True)
    guest_token: Mapped[str | None] = mapped_column(String(64), unique=True, index=True)
    guest_locale: Mapped[str | None] = mapped_column(String(5))

    # Order details
    order_number: Mapped[str] = mapped_column(String(50), unique=True, index=True)
    status: Mapped[OrderStatus] = mapped_column(SQLEnum(OrderStatus), default=OrderStatus.PENDING)

    # Pricing
    subtotal: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    tax: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"))
    shipping: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"))
    # Coupon discount applied to the goods subtotal (never to shipping). 0 when no
    # coupon was redeemed. ``total == quantize_money(subtotal - discount + shipping)``.
    discount: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), default=Decimal("0"), server_default="0"
    )
    total: Mapped[Decimal] = mapped_column(Numeric(12, 2))
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

    # B2C faktura request ("Chcę fakturę / na firmę"). A consumer (or guest)
    # buying for their business supplies a NIP + the buyer name to print on the
    # faktura; both NULL on a plain consumer order. The NIP is checksum-validated
    # at the API boundary (``app/core/validators.py``). When set, these take
    # precedence over the user/guest identity in the invoice buyer block.
    invoice_nip: Mapped[str | None] = mapped_column(String(16))
    invoice_name: Mapped[str | None] = mapped_column(String(200))

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

    # InPost Paczkomat (parcel locker) selection — set when the chosen method is
    # an InPost locker (the storefront Geowidget yields the point); NULL for
    # courier-to-address. ``inpost_point_id`` is the locker code (e.g. KRA010);
    # ``inpost_point_name`` snapshots its human address for the summary/label.
    inpost_point_id: Mapped[str | None] = mapped_column(String(32))
    inpost_point_name: Mapped[str | None] = mapped_column(String(255))

    # Shipment tracking (all nullable — recorded by the admin when the order
    # ships, editable afterwards; pre-shipping orders and untracked carriers
    # carry none). ``tracking_url`` is stored verbatim when the admin provides
    # one — there is deliberately no carrier→URL-template guessing table (v1).
    shipping_carrier: Mapped[str | None] = mapped_column(String(100))
    tracking_number: Mapped[str | None] = mapped_column(String(100))
    tracking_url: Mapped[str | None] = mapped_column(String(500))

    # When the order was marked delivered (naive UTC). Set by
    # ``apply_admin_update`` on the transition into DELIVERED; anchors the
    # returns-eligibility window. NULL on legacy delivered orders — the
    # eligibility check falls back to ``updated_at`` for those.
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime)

    # Notes
    notes: Mapped[str | None] = mapped_column(Text)

    # Relationships (``user`` is None on guest orders)
    user: Mapped["User | None"] = relationship("User", back_populates="orders")
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
        """Email of the ordering user (guest orders fall back to ``guest_email``)."""
        return self.user.email if self.user else self.guest_email


class OrderItem(BaseModel):
    """Order item model (products in an order)"""

    __tablename__ = "order_items"

    # Order
    order_id: Mapped[int] = mapped_column(Integer, ForeignKey("orders.id"))

    # Product
    product_id: Mapped[int] = mapped_column(Integer, ForeignKey("products.id"))
    # Optional variant ref — SET NULL on variant deletion so the order line
    # survives with its snapshots (same discipline as coupon_code /
    # shipping_method_name). NULL for variantless products.
    variant_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("product_variants.id", ondelete="SET NULL"), index=True
    )

    # Item details (snapshot at time of order)
    product_name: Mapped[str] = mapped_column(String(200))
    # Variant title snapshot ("Red / XL") — set iff the line was ordered as a
    # variant; outlives the variant row (SET NULL keeps history readable).
    variant_title: Mapped[str | None] = mapped_column(String(200))
    product_sku: Mapped[str | None] = mapped_column(String(100))
    quantity: Mapped[int] = mapped_column(Integer)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    total_price: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    # VAT rate (%) snapshotted from the product at purchase time, so a faktura
    # reissued after the product's rate changes still shows what was sold.
    vat_rate: Mapped[int] = mapped_column(Integer, default=23, server_default="23")

    # Relationships
    order: Mapped["Order"] = relationship("Order", back_populates="items")
    product: Mapped["Product"] = relationship("Product", back_populates="order_items")

    def __repr__(self):
        return f"<OrderItem {self.product_name} x{self.quantity}>"
