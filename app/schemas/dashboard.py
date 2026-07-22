"""Admin dashboard schemas — the KPI payload for `GET /api/v1/admin/dashboard/kpis`.

Every money field is a **per-currency dict** (``{currency: amount}``) — the store
sells in USD/PLN/EUR and totals are never summed across currencies (same shape as
``crud_user.order_metrics``'s ``total_spent``). "Revenue" counts only orders in
``REVENUE_STATUSES`` (paid/processing/shipped/delivered); see ``app/crud/dashboard.py``.
"""

from datetime import date, datetime
from typing import Literal

from app.schemas.base import BaseSchema
from app.schemas.inventory import InventoryStatus


class SalesTrendPoint(BaseSchema):
    """One day in the sales trend (zero-filled across the whole window).

    ``order_count`` is every order placed that day (volume); ``revenue`` is the
    per-currency sum of revenue-status orders placed that day. Both attributed by
    ``created_at``.
    """

    date: date
    order_count: int
    revenue: dict[str, float]


class TopProductItem(BaseSchema):
    """A best-seller, ranked by units sold over revenue-status orders.

    ``name``/``slug`` are the product's current values; ``name`` falls back to the
    ``order_items`` snapshot if the product row was deleted, and ``slug`` is then null.
    """

    product_id: int
    name: str
    slug: str | None = None
    units_sold: int
    order_count: int


class LowStockItem(BaseSchema):
    """A product needing attention (computed status is not ``in_stock``)."""

    product_id: int
    name: str
    sku: str | None = None
    stock: int
    low_stock_threshold: int | None = None
    status: InventoryStatus


class EuTaxLiability(BaseSchema):
    """Compact EU distance-selling threshold status for the dashboard nudge.

    ``status`` is the worse of the current + preceding year (so a prior-year
    crossing still shows); ``pct`` + ``year`` are the current year's. The frontend
    surfaces the badge only when ``status`` is not ``not_approaching``. Full detail
    lives at ``GET /api/v1/admin/reports/eu-distance-selling``.
    """

    status: Literal["not_approaching", "approaching", "threshold_met"]
    pct: float
    year: int
    liable: bool


class DashboardKpis(BaseSchema):
    """The full admin-dashboard payload (read-only aggregate)."""

    window_days: int
    generated_at: datetime
    primary_currency: str

    # Revenue + average order value, per currency (revenue-status orders).
    revenue_total: dict[str, float]
    revenue_window: dict[str, float]
    aov_total: dict[str, float]

    # Order counts.
    orders_total: int
    orders_window: int
    orders_pending: int

    # Customers (role=CUSTOMER).
    customers_total: int
    customers_window: int

    # Detail blocks.
    sales_trend: list[SalesTrendPoint]
    top_products: list[TopProductItem]
    low_stock_count: int
    low_stock: list[LowStockItem]

    # EU distance-selling €10 000 threshold nudge (Shopify-style tax-liability).
    eu_tax_liability: EuTaxLiability
