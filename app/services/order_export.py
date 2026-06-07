"""Order CSV export (RFC 4180; UTF-8 with BOM for Excel + Polish diacritics).

Pure: ``build_orders_csv`` turns an in-memory sequence of orders (with
eager-loaded ``items`` + ``user``) into CSV bytes. No DB access and no Settings
dependency, so it is unit-testable in isolation like ``invoice.py``.

Shape: **one row per order** (the bookkeeping/ops file — the money columns stay
safely summable). Line items are packed into a single ``items`` column
(``"2× Widget (WD-1); 1× Gadget"``) plus an ``item_count`` so the file stays
flat. A line-item-level (Shopify-style) export is a deliberate future variant.

Format choices (matter for EN/PL data tooling):
- RFC 4180 — comma delimiter, ``\\r\\n`` terminator, ``QUOTE_MINIMAL`` (the
  ``csv`` module's ``excel`` dialect default; it quotes any cell containing a
  comma/quote/newline, so packed items, notes, and addresses round-trip).
- **UTF-8 with BOM** (``utf-8-sig``) so Excel on Windows renders ``ł/ą/ę``.
- Stable English snake_case headers (locale-independent — data tools expect
  stable column names); status stays the raw enum value (``paid``/``shipped``).
- Machine-readable numbers (``f"{v:.2f}"``, dot-decimal, no symbols); the ISO
  currency code lives in its own column. ISO-8601 dates marked ``Z`` (UTC) since
  ``created_at`` is naive UTC.
"""

from __future__ import annotations

import csv
import io
from typing import TYPE_CHECKING

from app.services._csv_safe import csv_safe

if TYPE_CHECKING:
    from collections.abc import Sequence

    from app.models.order import Order

# Column order is part of the contract — downstream tools key on these names.
# ``payment_intent_id`` is intentionally omitted (internal provider id, low value).
CSV_HEADERS: list[str] = [
    "order_number",
    "created_at",
    "status",
    "customer_email",
    "customer_name",
    "currency",
    "subtotal",
    "tax",
    "shipping",
    "discount",
    "coupon_code",
    "total",
    "item_count",
    "items",
    "payment_method",
    "shipping_address",
    "shipping_city",
    "shipping_postal_code",
    "shipping_country",
    "notes",
]


def _status_value(order: Order) -> str:
    """Raw enum value (``"paid"``) whether status arrives as a member or a string."""
    return order.status.value if hasattr(order.status, "value") else str(order.status)


def _pack_items(order: Order) -> str:
    """``"2× Widget — Red / XL (WD-1); 1× Gadget"`` — qty × name (variant title
    appended when the line was ordered as a variant), SKU in parens when present
    (``product_sku`` already snapshots the variant SKU on variant lines)."""
    parts: list[str] = []
    for item in order.items:
        label = f"{item.quantity}× {item.product_name}"
        if item.variant_title:
            label += f" — {item.variant_title}"
        if item.product_sku:
            label += f" ({item.product_sku})"
        parts.append(label)
    return "; ".join(parts)


def _customer_name(order: Order) -> str:
    """``"First Last"`` from the ordering user; blank if the order has no user."""
    user = order.user
    if user is None:
        return ""
    return f"{user.first_name} {user.last_name}".strip()


def _created_at(order: Order) -> str:
    """ISO-8601 to the second with a ``Z`` suffix (``created_at`` is naive UTC)."""
    if order.created_at is None:
        return ""
    return order.created_at.replace(microsecond=0).isoformat() + "Z"


def _row(order: Order) -> list[str]:
    # User-controlled free-text cells go through ``csv_safe`` (formula-injection
    # guard); numeric/date/enum/currency cells are server-derived and left as-is.
    return [
        csv_safe(order.order_number),
        _created_at(order),
        _status_value(order),
        csv_safe(order.customer_email or ""),
        csv_safe(_customer_name(order)),
        order.currency,
        f"{order.subtotal:.2f}",
        f"{order.tax:.2f}",
        f"{order.shipping:.2f}",
        f"{(order.discount or 0):.2f}",
        csv_safe(order.coupon_code or ""),
        f"{order.total:.2f}",
        str(order.item_count),
        csv_safe(_pack_items(order)),
        csv_safe(order.payment_method or ""),
        csv_safe(order.shipping_address or ""),
        csv_safe(order.shipping_city or ""),
        csv_safe(order.shipping_postal_code or ""),
        csv_safe(order.shipping_country or ""),
        csv_safe(order.notes or ""),
    ]


def build_orders_csv(orders: Sequence[Order]) -> bytes:
    """Render orders to RFC 4180 CSV bytes (UTF-8 with BOM). Header-only if empty."""
    buf = io.StringIO()
    writer = csv.writer(buf)  # excel dialect: comma, \r\n, QUOTE_MINIMAL
    writer.writerow(CSV_HEADERS)
    for order in orders:
        writer.writerow(_row(order))
    return buf.getvalue().encode("utf-8-sig")
