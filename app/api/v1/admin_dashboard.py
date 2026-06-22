"""Admin dashboard API — aggregate KPIs for the `/admin` overview.

Admin-gated and **read-only**: no audit row, no commit (mirrors the admin
audit-log viewer). All aggregation lives in ``crud_dashboard``; this route just
exposes it. Money figures are per-currency dicts — never summed across currencies.
"""

from datetime import UTC, date, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.dependencies import get_current_admin, get_db
from app.crud import dashboard as crud_dashboard
from app.models.user import User
from app.schemas.dashboard import DashboardKpis

router = APIRouter()

# Cap a custom range at a year (inclusive) — mirrors the ``window_days`` upper bound
# and keeps the trend payload bounded.
MAX_RANGE_DAYS = 366


@router.get("/kpis", response_model=DashboardKpis)
async def get_dashboard_kpis(
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    window_days: int = Query(
        30, ge=1, le=365, description="Trailing window for the trend + windowed KPIs (days)"
    ),
    start_date: date | None = Query(
        None,
        description=(
            "Custom range start (inclusive). When given, overrides `window_days`; "
            "the window becomes [start_date, end_date]."
        ),
    ),
    end_date: date | None = Query(
        None, description="Custom range end (inclusive). Defaults to today."
    ),
):
    """Dashboard KPIs: revenue/orders/customers, sales trend, top products, low stock.

    Windowed figures cover a trailing `window_days` window by default, or an explicit
    `[start_date, end_date]` range when `start_date` is supplied.
    """
    if start_date is not None:
        effective_end = end_date or datetime.now(UTC).date()
        if start_date > effective_end:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="start_date must be on or before end_date.",
            )
        if (effective_end - start_date).days + 1 > MAX_RANGE_DAYS:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Date range must not exceed {MAX_RANGE_DAYS} days.",
            )

    return crud_dashboard.kpis(
        db, window_days=window_days, start_date=start_date, end_date=end_date
    )
