"""Admin dashboard API — aggregate KPIs for the `/admin` overview.

Admin-gated and **read-only**: no audit row, no commit (mirrors the admin
audit-log viewer). All aggregation lives in ``crud_dashboard``; this route just
exposes it. Money figures are per-currency dicts — never summed across currencies.
"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.dependencies import get_current_admin, get_db
from app.crud import dashboard as crud_dashboard
from app.models.user import User
from app.schemas.dashboard import DashboardKpis

router = APIRouter()


@router.get("/kpis", response_model=DashboardKpis)
async def get_dashboard_kpis(
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    window_days: int = Query(
        30, ge=1, le=365, description="Window for the trend + windowed KPIs (days)"
    ),
):
    """Dashboard KPIs: revenue/orders/customers, sales trend, top products, low stock."""
    return crud_dashboard.kpis(db, window_days=window_days)
