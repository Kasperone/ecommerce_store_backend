"""Dashboard CRUD: read-only aggregations powering `GET /admin/dashboard/kpis`.

Cross-entity reads over orders, order items, products, and users. Nothing here
mutates or commits. Two invariants shape the numbers:

- **Revenue counts only collected orders** — ``REVENUE_STATUSES`` (paid, processing,
  shipped, delivered). ``pending`` is not revenue yet; ``cancelled``/``refunded`` are
  excluded. Pending volume is surfaced separately as an actionable count.
- **Money is never summed across currencies.** Every money figure is a per-currency
  dict (the store sells in USD/PLN/EUR), matching ``crud_user.order_metrics``.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy import and_, case, func, or_
from sqlalchemy.orm import Session

from app.crud.inventory import inventory as crud_inventory
from app.models.order import Order, OrderItem, OrderStatus
from app.models.product import Product
from app.models.user import User, UserRole

# Statuses where money was actually collected — the definition of "revenue".
REVENUE_STATUSES: tuple[OrderStatus, ...] = (
    OrderStatus.PAID,
    OrderStatus.PROCESSING,
    OrderStatus.SHIPPED,
    OrderStatus.DELIVERED,
)


def _naive_utcnow() -> datetime:
    """Naive-UTC now (project convention — all datetime columns are naive UTC)."""
    return datetime.now(UTC).replace(tzinfo=None)


class CRUDDashboard:
    """Admin-dashboard read aggregations (no commit; pure reads)."""

    # ------------------------------------------------------------------
    # Revenue / orders / customers
    # ------------------------------------------------------------------

    def revenue_stats(
        self, db: Session, *, since: datetime | None = None, until: datetime | None = None
    ) -> dict[str, dict[str, float]]:
        """Per-currency revenue + order count over revenue-status orders.

        ``{currency: {"revenue": float, "orders": int}}``. ``since`` / ``until`` (if
        given) bound by ``created_at >=`` / ``created_at <=`` (inclusive window).
        """
        query = db.query(
            Order.currency,
            func.coalesce(func.sum(Order.total), 0.0),
            func.count(Order.id),
        ).filter(Order.status.in_(REVENUE_STATUSES))
        if since is not None:
            query = query.filter(Order.created_at >= since)
        if until is not None:
            query = query.filter(Order.created_at <= until)
        rows = query.group_by(Order.currency).all()
        return {
            currency: {"revenue": round(float(revenue or 0.0), 2), "orders": int(orders)}
            for currency, revenue, orders in rows
        }

    def order_count(
        self,
        db: Session,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
        status: OrderStatus | None = None,
    ) -> int:
        """Count orders, optionally filtered by ``status`` and a ``created_at`` window."""
        query = db.query(func.count(Order.id))
        if status is not None:
            query = query.filter(Order.status == status)
        if since is not None:
            query = query.filter(Order.created_at >= since)
        if until is not None:
            query = query.filter(Order.created_at <= until)
        return query.scalar() or 0

    def customer_count(
        self, db: Session, *, since: datetime | None = None, until: datetime | None = None
    ) -> int:
        """Count customer-role users, optionally bounded by a ``created_at`` window."""
        query = db.query(func.count(User.id)).filter(User.role == UserRole.CUSTOMER)
        if since is not None:
            query = query.filter(User.created_at >= since)
        if until is not None:
            query = query.filter(User.created_at <= until)
        return query.scalar() or 0

    # ------------------------------------------------------------------
    # Sales trend
    # ------------------------------------------------------------------

    def sales_trend(
        self,
        db: Session,
        *,
        days: int | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> list[dict]:
        """Zero-filled daily trend over an inclusive date range.

        Pass either ``days`` (a trailing window ending ``end_date`` or today) or an
        explicit ``start_date`` (with optional ``end_date``, default today) for an
        arbitrary range. Each point: ``{"date", "order_count", "revenue": {currency:
        amount}}`` — ``order_count`` is every order placed that day; ``revenue`` sums
        only revenue-status orders, per currency. Attributed by ``created_at``.
        """
        end = end_date or _naive_utcnow().date()
        start = start_date if start_date is not None else end - timedelta(days=(days or 30) - 1)
        total_days = (end - start).days + 1
        start_dt = datetime.combine(start, time.min)
        end_dt = datetime.combine(end, time.max)

        day_col = func.date(Order.created_at)
        revenue_sum = func.coalesce(
            func.sum(case((Order.status.in_(REVENUE_STATUSES), Order.total), else_=0.0)),
            0.0,
        )
        rows = (
            db.query(day_col, Order.currency, func.count(Order.id), revenue_sum)
            .filter(Order.created_at >= start_dt, Order.created_at <= end_dt)
            .group_by(day_col, Order.currency)
            .all()
        )

        per_day: dict[date, dict] = {}
        for day, currency, count, revenue in rows:
            bucket = per_day.setdefault(day, {"order_count": 0, "revenue": {}})
            bucket["order_count"] += int(count)
            if revenue:
                bucket["revenue"][currency] = round(float(revenue), 2)

        trend: list[dict] = []
        for offset in range(total_days):
            day = start + timedelta(days=offset)
            bucket = per_day.get(day, {"order_count": 0, "revenue": {}})
            trend.append(
                {
                    "date": day,
                    "order_count": bucket["order_count"],
                    "revenue": bucket["revenue"],
                }
            )
        return trend

    # ------------------------------------------------------------------
    # Top products
    # ------------------------------------------------------------------

    def top_products(self, db: Session, *, limit: int = 5) -> list[dict]:
        """Best-sellers by units sold over revenue-status orders.

        Carries the current product name + slug (slug null if the product was
        deleted), falling back to the ``order_items`` snapshot name.
        """
        rows = (
            db.query(
                OrderItem.product_id,
                func.sum(OrderItem.quantity).label("units"),
                func.count(func.distinct(OrderItem.order_id)).label("orders"),
                func.max(OrderItem.product_name).label("snapshot_name"),
            )
            .join(Order, OrderItem.order_id == Order.id)
            .filter(Order.status.in_(REVENUE_STATUSES))
            .group_by(OrderItem.product_id)
            .order_by(func.sum(OrderItem.quantity).desc(), OrderItem.product_id.asc())
            .limit(limit)
            .all()
        )
        if not rows:
            return []

        product_ids = [product_id for product_id, *_ in rows]
        products = {
            p.id: p
            for p in db.query(Product.id, Product.name, Product.slug).filter(
                Product.id.in_(product_ids)
            )
        }
        result: list[dict] = []
        for product_id, units, orders, snapshot_name in rows:
            product = products.get(product_id)
            result.append(
                {
                    "product_id": product_id,
                    "name": product.name if product else snapshot_name,
                    "slug": product.slug if product else None,
                    "units_sold": int(units or 0),
                    "order_count": int(orders or 0),
                }
            )
        return result

    # ------------------------------------------------------------------
    # Low stock
    # ------------------------------------------------------------------

    def _not_in_stock_filter(self):
        """Predicate matching every non-``in_stock`` product (out / backorder / low)."""
        return or_(
            Product.stock <= 0,  # out_of_stock or backorder
            and_(
                Product.low_stock_threshold.isnot(None),
                Product.stock > 0,
                Product.stock <= Product.low_stock_threshold,
            ),
        )

    def low_stock(self, db: Session, *, limit: int = 5) -> tuple[int, list[Product]]:
        """Count + most-urgent sample of products needing attention (stock asc).

        Mirrors the ``/admin/inventory`` view (no ``is_active`` filter); the route
        renders ``crud_inventory.status_for`` per product.
        """
        predicate = self._not_in_stock_filter()
        count = db.query(func.count(Product.id)).filter(predicate).scalar() or 0
        products = (
            db.query(Product)
            .filter(predicate)
            .order_by(Product.stock.asc(), Product.name.asc(), Product.id.asc())
            .limit(limit)
            .all()
        )
        return count, products

    # ------------------------------------------------------------------
    # Orchestrator
    # ------------------------------------------------------------------

    def kpis(
        self,
        db: Session,
        *,
        window_days: int = 30,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> dict:
        """Assemble the full dashboard payload (matches ``schemas.DashboardKpis``).

        The windowed figures (``*_window`` + ``sales_trend``) cover an inclusive date
        range. By default it's a trailing window of ``window_days`` ending today; pass
        ``start_date`` (with optional ``end_date``, default today) for a custom range.
        ``window_days`` in the payload is always the effective inclusive span, so the
        UI's "in the last N days" copy stays accurate either way. The all-time figures
        (``*_total``, ``orders_pending``, ``top_products``, ``low_stock``) are never
        windowed.
        """
        now = _naive_utcnow()
        today = now.date()
        range_end = end_date or today
        range_start = (
            start_date if start_date is not None else range_end - timedelta(days=window_days - 1)
        )
        effective_days = (range_end - range_start).days + 1
        window_start = datetime.combine(range_start, time.min)
        window_end = datetime.combine(range_end, time.max)

        total_stats = self.revenue_stats(db)
        window_stats = self.revenue_stats(db, since=window_start, until=window_end)

        revenue_total = {currency: s["revenue"] for currency, s in total_stats.items()}
        revenue_window = {currency: s["revenue"] for currency, s in window_stats.items()}
        aov_total = {
            currency: round(s["revenue"] / s["orders"], 2)
            for currency, s in total_stats.items()
            if s["orders"]
        }
        primary_currency = (
            max(revenue_total, key=lambda c: revenue_total[c]) if revenue_total else "USD"
        )

        # Imported here (not at module scope) to avoid a crud↔services import cycle
        # (``tax_threshold`` imports ``crud.report`` / ``crud.store_config``).
        from app.services import tax_threshold

        liability = tax_threshold.eu_distance_selling_status(db)
        eu_tax_liability = {
            "status": liability.effective_status,
            "pct": float(liability.current.pct),
            "year": liability.current.year,
            "liable": liability.liable,
        }

        low_stock_count, low_stock_products = self.low_stock(db)
        low_stock = [
            {
                "product_id": p.id,
                "name": p.name,
                "sku": p.sku,
                "stock": p.stock,
                "low_stock_threshold": p.low_stock_threshold,
                "status": crud_inventory.status_for(p),
            }
            for p in low_stock_products
        ]

        return {
            "window_days": effective_days,
            "generated_at": now,
            "primary_currency": primary_currency,
            "revenue_total": revenue_total,
            "revenue_window": revenue_window,
            "aov_total": aov_total,
            "orders_total": self.order_count(db),
            "orders_window": self.order_count(db, since=window_start, until=window_end),
            "orders_pending": self.order_count(db, status=OrderStatus.PENDING),
            "customers_total": self.customer_count(db),
            "customers_window": self.customer_count(db, since=window_start, until=window_end),
            "sales_trend": self.sales_trend(db, start_date=range_start, end_date=range_end),
            "top_products": self.top_products(db),
            "low_stock_count": low_stock_count,
            "low_stock": low_stock,
            "eu_tax_liability": eu_tax_liability,
        }


dashboard = CRUDDashboard()
