"""Admin reports API — read-only aggregates for the `/admin` reporting views.

Admin-gated and **read-only**: no audit row, no commit (mirrors
``app/api/v1/admin_dashboard.py`` and the audit-log viewer). All aggregation
lives in ``crud_report``; these routes just expose it and shape the response.
Money figures are per-currency dicts — never summed across currencies.

Endpoints (all under ``/api/v1/admin/reports``):

- ``GET /sales`` — zero-filled time series (day/week/month buckets).
- ``GET /top-products`` — best-sellers by units, with per-currency revenue.
- ``GET /by-category`` — units + per-currency revenue per category.
- ``GET /refund-summary`` — per-currency refunded total + count + rate.
- ``GET /sales.csv`` — the sales series as an RFC 4180 CSV (UTF-8 with BOM).
"""

from __future__ import annotations

import csv
import io
from datetime import date

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.orm import Session

from app.core.dependencies import get_current_admin, get_db
from app.crud import report as crud_report
from app.models.user import User
from app.schemas.report import (
    CategoryRevenueReport,
    EuDistanceSellingStatus,
    RefundSummary,
    SalesGranularity,
    SalesReport,
    TopProductsReport,
)
from app.services import tax_threshold

router = APIRouter()


# Sales CSV columns are part of the contract — currency columns are emitted in a
# stable order. The packed ``revenue_<CUR>`` columns mirror the per-currency dict
# so the file stays flat (one row per bucket, like the orders export).
_CSV_CURRENCIES: tuple[str, ...] = ("USD", "PLN", "EUR")


def _sales_csv(buckets: list[dict]) -> bytes:
    """Render the sales series to RFC 4180 CSV bytes (UTF-8 with BOM).

    One row per bucket: ``bucket, orders, revenue_USD, revenue_PLN, revenue_EUR``.
    Header-only if empty. Per-currency money stays in its own column (never
    summed). Mirrors ``app/services/order_export.py``'s format choices: ``excel``
    dialect (comma, ``\\r\\n``, ``QUOTE_MINIMAL``) + ``utf-8-sig`` BOM for Excel.
    """
    headers = ["bucket", "orders", *[f"revenue_{c}" for c in _CSV_CURRENCIES]]
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(headers)
    for bucket in buckets:
        revenue = bucket["revenue"]
        writer.writerow(
            [
                bucket["bucket"].isoformat(),
                str(bucket["orders"]),
                *[f"{revenue.get(c, 0.0):.2f}" for c in _CSV_CURRENCIES],
            ]
        )
    return buf.getvalue().encode("utf-8-sig")


@router.get("/sales", response_model=SalesReport)
async def get_sales_report(
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    start: date = Query(..., description="Range start (UTC calendar day, inclusive)"),
    end: date = Query(..., description="Range end (UTC calendar day, inclusive)"),
    granularity: SalesGranularity = Query("day", description="Bucket size: day|week|month"),
):
    """Sales over time — per-bucket revenue (per currency) + order count.

    Counts only revenue-status orders (paid/processing/shipped/delivered),
    attributed by ``created_at``. Buckets are zero-filled across the range.
    """
    buckets = crud_report.sales_over_time(db, start=start, end=end, granularity=granularity)
    return {"start": start, "end": end, "granularity": granularity, "buckets": buckets}


@router.get("/top-products", response_model=TopProductsReport)
async def get_top_products_report(
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    start: date | None = Query(None, description="Range start (UTC day, inclusive)"),
    end: date | None = Query(None, description="Range end (UTC day, inclusive)"),
    limit: int = Query(10, ge=1, le=100, description="Max rows"),
):
    """Best-sellers by units sold over revenue-status orders in the range."""
    rows = crud_report.top_products(db, start=start, end=end, limit=limit)
    return {"start": start, "end": end, "limit": limit, "rows": rows}


@router.get("/by-category", response_model=CategoryRevenueReport)
async def get_category_revenue_report(
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    start: date | None = Query(None, description="Range start (UTC day, inclusive)"),
    end: date | None = Query(None, description="Range end (UTC day, inclusive)"),
):
    """Units + per-currency revenue per category over revenue-status orders.

    Attribution follows the product's current category; products with no
    category bucket under ``Uncategorized``.
    """
    rows = crud_report.revenue_by_category(db, start=start, end=end)
    return {"start": start, "end": end, "rows": rows}


@router.get("/refund-summary", response_model=RefundSummary)
async def get_refund_summary_report(
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    start: date | None = Query(None, description="Range start (UTC day, inclusive)"),
    end: date | None = Query(None, description="Range end (UTC day, inclusive)"),
):
    """Per-currency refunded total + count + rate (refunded / revenue) over the range."""
    summary = crud_report.refund_summary(db, start=start, end=end)
    return {"start": start, "end": end, **summary}


@router.get("/eu-distance-selling", response_model=EuDistanceSellingStatus)
async def get_eu_distance_selling_status(
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    year: int | None = Query(
        None, ge=2000, le=2100, description="Calendar year (defaults to the current year)"
    ),
):
    """EU distance-selling €10 000 threshold status for the year + preceding year.

    Sums cross-border B2C net-of-VAT sales (EU destinations ≠ the home country) in
    EUR and returns a Shopify-style tiered status so the OSS crossing is detected
    on data, not discovered on an audit. Reverse-charge (B2B) and domestic sales
    are excluded; see ``app/services/tax_threshold.py``.
    """
    return tax_threshold.eu_distance_selling_status(db, year=year)


@router.get(
    "/sales.csv",
    responses={200: {"content": {"text/csv": {}}, "description": "Sales report CSV export"}},
)
async def export_sales_csv(
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    start: date = Query(..., description="Range start (UTC calendar day, inclusive)"),
    end: date = Query(..., description="Range end (UTC calendar day, inclusive)"),
    granularity: SalesGranularity = Query("day", description="Bucket size: day|week|month"),
):
    """Download the sales series as a CSV file (admin only).

    One row per bucket with a column per currency; RFC 4180, UTF-8 with BOM.
    Generated on demand — no persistence, no audit row (read-only GET, like the
    orders/invoice downloads).
    """
    buckets = crud_report.sales_over_time(db, start=start, end=end, granularity=granularity)
    csv_bytes = _sales_csv(buckets)
    filename = f"sales-{start}_{end}-{granularity}.csv"
    return Response(
        content=csv_bytes,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
