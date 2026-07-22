from datetime import datetime
from typing import Literal

from pydantic import EmailStr, Field, field_validator

from app.core.countries import CountryCode
from app.core.validators import is_valid_nip, normalize_nip
from app.models.order import OrderStatus
from app.schemas.base import BaseSchema, TimestampSchema
from app.schemas.refund import RefundResponse
from app.schemas.user import UserResponse


def _blank_to_none(v: object) -> object:
    """Trim; a blank/whitespace-only string → null (clears the field). Non-strings
    pass through untouched so type validation still rejects them."""
    if isinstance(v, str):
        stripped = v.strip()
        return stripped if stripped else None
    return v


class OrderItemBase(BaseSchema):
    """Base order item schema"""

    product_id: int
    # Optional variant selection. Required (enforced in the CRUD layer, 422)
    # when the product has active variants; rejected when the variant is
    # missing/inactive/another product's. NULL for variantless products.
    variant_id: int | None = None
    quantity: int = Field(..., gt=0)


class OrderItemCreate(OrderItemBase):
    """Schema for creating an order item"""

    pass


class OrderItemResponse(OrderItemBase, TimestampSchema):
    """Schema for order item response"""

    order_id: int
    product_name: str
    # Variant title snapshot ("Red / XL") — null for variantless lines.
    # Additive; powers the order-detail display + reorder ("buy again").
    variant_title: str | None = None
    product_sku: str | None
    unit_price: float
    total_price: float


class OrderBase(BaseSchema):
    """Base order schema.

    ``shipping_country`` is deliberately **not** here: placing an order requires
    a destination (``OrderCreate``), but reading one back may find none — a
    quote-converted order has no address until an admin sets it at fulfilment
    (``OrderResponse``). The two sides genuinely disagree, so each declares its
    own rather than one widening the other.
    """

    # Shipping address
    shipping_address: str = Field(..., min_length=1)
    shipping_city: str = Field(..., min_length=1, max_length=100)
    shipping_postal_code: str = Field(..., min_length=1, max_length=20)

    # Optional fields
    notes: str | None = None


class OrderCreate(OrderBase):
    """Schema for creating an order"""

    shipping_country: CountryCode
    items: list[OrderItemCreate] = Field(..., min_length=1)
    currency: str = Field(default="USD", pattern="^(USD|PLN|EUR)$")
    # The shipping method the customer picked from the storefront selector.
    # Required — the order route 422s when no rate exists for the chosen
    # method / country / currency triple (ShippingRateUnavailableError).
    shipping_method_id: int
    # Optional discount code. Re-validated server-side against the computed
    # subtotal at create time (the storefront preview is advisory only); an
    # invalid/expired/exhausted code 400s the create with the reason message.
    coupon_code: str | None = Field(default=None, max_length=64)
    # Guest checkout: contact email for an anonymous order. The route REQUIRES
    # it when no (valid) bearer is present (422 otherwise) and REJECTS it when
    # authenticated (422 — the account email is authoritative, no ambiguity).
    guest_email: EmailStr | None = None
    # Guest email language (stored as ``orders.guest_locale``); ignored for
    # authenticated orders (their ``User.locale`` preference wins).
    locale: Literal["en", "pl"] | None = None
    # B2C faktura request ("Chcę fakturę / na firmę"). Optional: a consumer or
    # guest buying for their business supplies the company name + NIP to print on
    # the faktura. The NIP is checksum-validated (a typo'd NIP on a legal invoice
    # is rejected); whitespace/dashes are normalized to digits on the way in.
    invoice_nip: str | None = Field(default=None, max_length=16)
    invoice_name: str | None = Field(default=None, max_length=200)
    # InPost Paczkomat selection from the storefront Geowidget. Required by the
    # order service when the chosen shipping method is an InPost locker (422
    # otherwise); ignored for courier-to-address methods.
    inpost_point_id: str | None = Field(default=None, max_length=32)
    inpost_point_name: str | None = Field(default=None, max_length=255)
    # Payment methods chosen AT create. ``"cod"`` = cash-on-delivery (pobranie);
    # ``"net_terms"`` = B2B pay-on-account (order placed unpaid with a printed due
    # date; eligibility + credit limit enforced server-side — 409 machine codes
    # ``net_terms_unavailable`` / ``net_terms_currency_mismatch`` /
    # ``credit_limit_exceeded``). Online methods (stripe/payu) are chosen AFTER
    # create on the payment page, never here. ``None`` = a normal online order.
    payment_method: Literal["cod", "net_terms"] | None = None
    # Stored-value tenders (D2/D10, applied after the coupon). A typed gift-card
    # code is validated server-side and fails LOUD (400 machine code, like a bad
    # coupon); ``use_store_credit`` is opportunistic — it applies
    # ``min(balance, remainder)`` and contributes 0 when there is nothing to
    # spend. Both are gated on the store_config toggles (409 when off);
    # ``use_store_credit`` additionally requires an account (400 on guest).
    gift_card_code: str | None = Field(default=None, max_length=64)
    use_store_credit: bool = False

    @field_validator("invoice_nip", mode="before")
    @classmethod
    def _validate_nip(cls, value: str | None) -> str | None:
        if value is None or not str(value).strip():
            return None
        normalized = normalize_nip(str(value))
        if not is_valid_nip(normalized):
            raise ValueError("Invalid NIP (Polish tax identification number).")
        return normalized


class OrderUpdate(BaseSchema):
    """Schema for updating an order (admin only)"""

    status: OrderStatus | None = None
    payment_method: str | None = None
    payment_intent_id: str | None = None
    notes: str | None = None
    # Shipment tracking — typically set alongside ``status=shipped`` and editable
    # afterwards. Values are trimmed; an explicit blank/empty string clears the
    # field (null). ``tracking_url`` is taken verbatim when provided — no
    # carrier→URL guessing (v1).
    shipping_carrier: str | None = Field(default=None, max_length=100)
    tracking_number: str | None = Field(default=None, max_length=100)
    tracking_url: str | None = Field(default=None, max_length=500)

    _trim_tracking = field_validator(
        "shipping_carrier", "tracking_number", "tracking_url", mode="before"
    )(_blank_to_none)


class OrderEditLine(BaseSchema):
    """One line in an order-edit request (the full desired line set is sent).

    ``unit_price_override`` NULL prices the line from the live catalog at the
    order's currency; a concrete value pins a negotiated/admin price (mirrors
    draft orders). Money is a wire ``float`` (converted to Decimal in the route).
    """

    product_id: int
    variant_id: int | None = None
    quantity: int = Field(..., gt=0)
    unit_price_override: float | None = Field(default=None, ge=0)


class OrderEditRequest(BaseSchema):
    """Request body for ``PATCH /api/v1/orders/{id}/items`` (admin, pending only).

    ``lines`` is the complete desired line set (not a delta). ``discount`` sets
    the order's manual discount (kept if omitted); ``shipping_method_id`` swaps
    the method (kept if omitted — shipping is re-resolved either way against the
    new lines).
    """

    lines: list[OrderEditLine] = Field(..., min_length=1)
    discount: float | None = Field(default=None, ge=0)
    shipping_method_id: int | None = None


class OrderResponse(OrderBase, TimestampSchema):
    """Schema for order response"""

    # Optional on the read side: a quote-converted order has no address yet and
    # carries NULL until an admin sets one at fulfilment (see ``OrderBase``).
    shipping_country: str | None = None

    # NULL on guest orders; ``guest_email``/``guest_token`` are then set instead.
    # The token rides the create response so the guest can be handed their
    # tracking URL (and start payment) — it is the order's own credential, only
    # ever returned to its creator (or an admin via the scoped detail).
    user_id: int | None = None
    guest_email: str | None = None
    guest_token: str | None = None
    order_number: str
    status: OrderStatus

    # Pricing
    subtotal: float
    tax: float
    shipping: float
    # Coupon discount applied to the subtotal (0 when none). ``coupon_code`` is the
    # redeemed code snapshot (null when no coupon was used).
    discount: float = 0.0
    coupon_code: str | None = None
    total: float
    currency: str

    # Stored-value tenders (D2): what a gift card / store credit already covered.
    # ``total`` is unchanged; ``amount_due = total - gift_card_amount -
    # store_credit_amount`` is what a provider still collects (0 = paid at
    # creation). ``gift_card_last4`` is the display snapshot (null when no card).
    gift_card_amount: float = 0.0
    store_credit_amount: float = 0.0
    amount_due: float
    gift_card_last4: str | None = None

    # B2C faktura request (null on a plain consumer order).
    invoice_nip: str | None = None
    invoice_name: str | None = None

    # Payment
    payment_method: str | None
    payment_intent_id: str | None

    # Tax treatment (Phase-0 tax U4/U5). ``domestic`` on every ordinary order;
    # ``reverse_charge`` when an intra-community B2B supply was priced at net (0%
    # VAT, the buyer self-accounts). ``buyer_vat_id`` is the snapshot the faktura
    # printed. The storefront reads these to show "reverse charge applied".
    tax_treatment: str = "domestic"
    buyer_vat_id: str | None = None

    # B2B net terms (Program 2). ``company_id`` attributes a pay-on-account order
    # to its company; ``payment_due_at`` is the printed ``termin płatności``;
    # ``net_terms_paid_at`` is set once the merchant records the bank transfer
    # (null = still outstanding). All null on non-net-terms orders.
    company_id: int | None = None
    payment_due_at: datetime | None = None
    net_terms_paid_at: datetime | None = None

    # Shipping method snapshot (null on pre-shipping legacy orders)
    shipping_method_id: int | None = None
    shipping_method_name: str | None = None

    # InPost Paczkomat selection (null for courier-to-address orders)
    inpost_point_id: str | None = None
    inpost_point_name: str | None = None

    # Shipment tracking (null until the admin records it at/after ship time)
    shipping_carrier: str | None = None
    tracking_number: str | None = None
    tracking_url: str | None = None

    # When the order was marked delivered (set by the admin PATCH on the
    # transition into ``delivered``; null before then and on legacy delivered
    # orders). Anchors the returns-eligibility window.
    delivered_at: datetime | None = None

    # Items
    items: list[OrderItemResponse] = Field(default_factory=list)

    # Refunds — additive; existing clients ignore. Populated by ``get_scoped``'s
    # eager-load. The admin surface uses this directly; the customer surface
    # re-shapes client-side to drop ``note`` (admin notes are internal).
    refunds: list[RefundResponse] = Field(default_factory=list)


class OrderUserResponse(UserResponse):
    """Nested owner for the order-detail view.

    Relaxes ``email`` to a plain ``str`` (``UserResponse.email`` is ``EmailStr``)
    so an order whose owner was GDPR-anonymized still serializes. The anonymizer
    sets the email to the reserved ``.invalid`` TLD
    (``deleted-user-<id>@anon.invalid``), which ``EmailStr`` rejects — without
    this relaxation ``GET /orders/{id}`` 500s with a ``ResponseValidationError``
    and the admin can't open the order at all. The value is still a string, just
    not RFC-deliverable; ``first_name``/``last_name`` ("deleted"/"user") stay
    visible so the admin sees *why* the row looks empty.
    """

    email: str


class OrderWithUser(OrderResponse):
    """Order response with user details (``user`` is null on guest orders)"""

    user: OrderUserResponse | None = None


class GuestOrderItem(BaseSchema):
    """Trimmed line item for the public guest-tracking view (snapshots only —
    no product/variant ids, nothing an enumeration could pivot on)."""

    product_name: str
    variant_title: str | None = None
    quantity: int
    unit_price: float
    total_price: float


class GuestOrderResponse(BaseSchema):
    """Public account-less order-tracking view (``GET /orders/track/{token}``).

    Deliberately trimmed: status, items, totals, shipping method name, shipment
    tracking fields, and the shipping city/country — no full street address, no
    payment ids, no refund detail, no user data. View only (no cancel — a
    stock-mutating action on token-possession-only auth is too weak).
    """

    order_number: str
    status: OrderStatus
    created_at: datetime
    items: list[GuestOrderItem] = Field(default_factory=list)
    subtotal: float
    discount: float = 0.0
    shipping: float
    total: float
    currency: str
    # Stored-value tenders — the guest payment step needs ``amount_due`` (and
    # the summary shows what the card/credit covered), same shape as
    # OrderResponse.
    gift_card_amount: float = 0.0
    store_credit_amount: float = 0.0
    amount_due: float
    gift_card_last4: str | None = None
    shipping_method_name: str | None = None
    shipping_carrier: str | None = None
    tracking_number: str | None = None
    tracking_url: str | None = None
    shipping_city: str
    shipping_country: str | None


class OrderListItem(TimestampSchema):
    """Lightweight order row for list views (no nested line items)."""

    order_number: str
    status: OrderStatus
    subtotal: float
    discount: float = 0.0
    total: float
    currency: str
    item_count: int
    customer_email: str | None = None
    payment_method: str | None = None

    # B2B net terms — surfaced on list rows so the admin AR/orders table can show
    # a due-date / OVERDUE badge and the pay-on-account state without a detail
    # fetch. Null on non-net-terms orders.
    company_id: int | None = None
    payment_due_at: datetime | None = None
    net_terms_paid_at: datetime | None = None

    # Shipment tracking — surfaced on list rows so the customer order history /
    # admin orders table can show a tracking link without a detail fetch.
    shipping_carrier: str | None = None
    tracking_number: str | None = None
    tracking_url: str | None = None


class OrderListResponse(BaseSchema):
    """Schema for paginated order list"""

    items: list[OrderListItem]
    total: int
    page: int
    page_size: int
    pages: int
