"""Admin reporting schemas — read-only aggregates over orders/items/products/refunds.

Every money field is a **per-currency dict** (``{currency: amount}``) — the store
sells in USD/PLN/EUR and totals are never summed across currencies (same shape as
``app/schemas/dashboard.py`` / ``crud_user.order_metrics``). "Revenue" counts only
orders in ``REVENUE_STATUSES`` (paid/processing/shipped/delivered); see
``app/crud/report.py``. All time attribution is by ``Order.created_at`` (naive UTC).
"""

from datetime import date
from typing import Literal

from app.schemas.base import BaseSchema

# Bucket granularity for the sales time series. ``week`` is ISO week (Mon-start),
# ``month`` the calendar month; each bucket is labelled by its first day.
SalesGranularity = Literal["day", "week", "month"]


class SalesBucket(BaseSchema):
    """One time bucket in the sales report.

    ``bucket`` is the first calendar day of the bucket (the day itself for
    ``day``, the Monday for ``week``, the 1st for ``month``). ``revenue`` is the
    per-currency sum of revenue-status orders created in the bucket; ``orders``
    is the count of those revenue-status orders. Buckets are zero-filled across
    the requested range so the series has no gaps.
    """

    bucket: date
    revenue: dict[str, float]
    orders: int


class SalesReport(BaseSchema):
    """The sales-over-time payload (read-only aggregate)."""

    start: date
    end: date
    granularity: SalesGranularity
    buckets: list[SalesBucket]


class TopProductRow(BaseSchema):
    """A best-seller over the window, ranked by units sold across revenue orders.

    ``name``/``slug`` are the product's current values; ``name`` falls back to the
    ``order_items`` snapshot if the product row was deleted, and ``slug`` is then
    null. ``revenue_by_currency`` is the per-currency sum of this product's line
    totals over revenue-status orders.
    """

    product_id: int
    name: str
    slug: str | None = None
    units: int
    order_count: int
    revenue_by_currency: dict[str, float]


class TopProductsReport(BaseSchema):
    """The top-products payload (read-only aggregate)."""

    start: date | None = None
    end: date | None = None
    limit: int
    rows: list[TopProductRow]


class CategoryRevenueRow(BaseSchema):
    """Revenue + units sold for one category over the window.

    Attribution follows the product's **current** ``category_id`` (a product's
    category can change; orders snapshot only name/sku, not the category).
    Products with no category are bucketed under ``category_id = null`` /
    ``name = "Uncategorized"``. ``revenue_by_currency`` is per-currency.
    """

    category_id: int | None = None
    name: str
    units: int
    revenue_by_currency: dict[str, float]


class CategoryRevenueReport(BaseSchema):
    """The by-category payload (read-only aggregate)."""

    start: date | None = None
    end: date | None = None
    rows: list[CategoryRevenueRow]


class RefundSummaryRow(BaseSchema):
    """Per-currency refund totals + the refund rate for one currency.

    ``refunded`` sums recorded refund amounts (``refunds.amount``); ``revenue`` is
    the comparable revenue-status order total in the same currency; ``rate`` is
    ``refunded / revenue`` (0.0 when there is no revenue). ``refund_count`` counts
    refund rows. Money is never summed across currencies, so the rate is computed
    per currency too.
    """

    currency: str
    refunded: float
    refund_count: int
    revenue: float
    rate: float


class RefundSummary(BaseSchema):
    """The refund-summary payload (read-only aggregate)."""

    start: date | None = None
    end: date | None = None
    refund_count: int
    rows: list[RefundSummaryRow]
