"""Report CRUD: read-only aggregations powering ``GET /admin/reports/*``.

Cross-entity reads over orders, order items, products, categories, and refunds.
Nothing here mutates or commits (mirrors ``app/crud/dashboard.py`` — read-only
means no audit row and no ``db.commit()``). Two invariants, identical to the
dashboard:

- **Revenue counts only collected orders** — ``REVENUE_STATUSES`` (paid,
  processing, shipped, delivered). ``pending`` is not revenue yet;
  ``cancelled``/``refunded``/``partially_refunded`` are excluded.
- **Money is never summed across currencies.** Every money figure is a
  per-currency dict (the store sells in USD/PLN/EUR).

Time attribution is by ``Order.created_at`` (naive UTC). Date-range filters
bound ``created_at`` by UTC calendar day with ``end`` **inclusive** (``< end +
1 day``) — the same convention as ``crud_order._apply_scope_and_filters`` so a
report and the orders list never drift.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

from sqlalchemy import func
from sqlalchemy.orm import Query, Session

from app.models.category import Category
from app.models.order import Order, OrderItem, OrderStatus
from app.models.product import Product
from app.models.refund import Refund
from app.schemas.report import SalesGranularity

# Statuses where money was actually collected — the definition of "revenue".
# Identical to ``crud_dashboard.REVENUE_STATUSES`` (partial/refunded excluded:
# those are surfaced by the refund summary, not as revenue).
REVENUE_STATUSES: tuple[OrderStatus, ...] = (
    OrderStatus.PAID,
    OrderStatus.PROCESSING,
    OrderStatus.SHIPPED,
    OrderStatus.DELIVERED,
)

UNCATEGORIZED_NAME = "Uncategorized"


def _apply_date_range(query: Query, column, *, start: date | None, end: date | None) -> Query:
    """Bound ``column`` by ``[start, end]`` as UTC calendar days (``end`` inclusive).

    ``created_at`` is naive UTC, so the boundaries are UTC days, not the admin's
    local timezone (mirrors ``crud_order._apply_scope_and_filters``).
    """
    if start is not None:
        query = query.filter(column >= datetime.combine(start, time.min))
    if end is not None:
        query = query.filter(column < datetime.combine(end, time.min) + timedelta(days=1))
    return query


def _bucket_floor(day: date, granularity: SalesGranularity) -> date:
    """First calendar day of the bucket containing ``day``.

    ``day`` → itself; ``week`` → the Monday (ISO week); ``month`` → the 1st.
    Mirrors Postgres ``date_trunc`` (week is Monday-based) so the Python
    zero-fill keys line up with the grouped query.
    """
    if granularity == "week":
        return day - timedelta(days=day.weekday())
    if granularity == "month":
        return day.replace(day=1)
    return day


def _next_bucket(bucket_start: date, granularity: SalesGranularity) -> date:
    """First day of the bucket after ``bucket_start`` (for zero-fill iteration)."""
    if granularity == "week":
        return bucket_start + timedelta(weeks=1)
    if granularity == "month":
        if bucket_start.month == 12:
            return bucket_start.replace(year=bucket_start.year + 1, month=1)
        return bucket_start.replace(month=bucket_start.month + 1)
    return bucket_start + timedelta(days=1)


class CRUDReport:
    """Admin-report read aggregations (no commit; pure reads)."""

    # ------------------------------------------------------------------
    # Sales over time
    # ------------------------------------------------------------------

    def sales_over_time(
        self,
        db: Session,
        *,
        start: date,
        end: date,
        granularity: SalesGranularity = "day",
    ) -> list[dict]:
        """Zero-filled per-bucket revenue + order count over the inclusive range.

        Each bucket: ``{"bucket": date, "revenue": {currency: amount}, "orders":
        int}``. ``orders`` counts revenue-status orders in the bucket; ``revenue``
        is their per-currency total. Buckets are zero-filled from
        ``_bucket_floor(start)`` to ``_bucket_floor(end)`` so the series has no
        gaps. Attribution by ``created_at``; ``end`` is inclusive.
        """
        # ``date_trunc`` returns a timestamp; cast to DATE for a clean group key.
        bucket_col = func.date(func.date_trunc(granularity, Order.created_at))
        query = db.query(
            bucket_col.label("bucket"),
            Order.currency,
            func.count(Order.id),
            func.coalesce(func.sum(Order.total), 0.0),
        ).filter(Order.status.in_(REVENUE_STATUSES))
        query = _apply_date_range(query, Order.created_at, start=start, end=end)
        rows = query.group_by("bucket", Order.currency).all()

        per_bucket: dict[date, dict] = {}
        for bucket, currency, orders, revenue in rows:
            entry = per_bucket.setdefault(bucket, {"orders": 0, "revenue": {}})
            entry["orders"] += int(orders)
            if revenue:
                # One order belongs to exactly one currency, so no cross-currency sum.
                entry["revenue"][currency] = round(float(revenue), 2)

        result: list[dict] = []
        cursor = _bucket_floor(start, granularity)
        last = _bucket_floor(end, granularity)
        while cursor <= last:
            entry = per_bucket.get(cursor, {"orders": 0, "revenue": {}})
            result.append(
                {"bucket": cursor, "revenue": entry["revenue"], "orders": entry["orders"]}
            )
            cursor = _next_bucket(cursor, granularity)
        return result

    # ------------------------------------------------------------------
    # Top products
    # ------------------------------------------------------------------

    def top_products(
        self,
        db: Session,
        *,
        start: date | None = None,
        end: date | None = None,
        limit: int = 10,
    ) -> list[dict]:
        """Best-sellers by units sold over revenue-status orders in the range.

        Each row: ``{"product_id", "name", "slug", "units", "order_count",
        "revenue_by_currency": {currency: amount}}``. ``name``/``slug`` are the
        product's current values (``name`` falls back to the ``order_items``
        snapshot if the product was deleted; ``slug`` is then null). Ranked by
        units desc, then product_id asc for a stable tie-break.
        """
        units_col = func.sum(OrderItem.quantity)
        agg = (
            db.query(
                OrderItem.product_id,
                units_col.label("units"),
                func.count(func.distinct(OrderItem.order_id)).label("orders"),
                func.max(OrderItem.product_name).label("snapshot_name"),
            )
            .join(Order, OrderItem.order_id == Order.id)
            .filter(Order.status.in_(REVENUE_STATUSES))
        )
        agg = _apply_date_range(agg, Order.created_at, start=start, end=end)
        rows = (
            agg.group_by(OrderItem.product_id)
            .order_by(units_col.desc(), OrderItem.product_id.asc())
            .limit(limit)
            .all()
        )
        if not rows:
            return []

        product_ids = [product_id for product_id, *_ in rows]

        # Per-currency revenue per product (line totals grouped by order currency).
        rev_query = (
            db.query(
                OrderItem.product_id,
                Order.currency,
                func.coalesce(func.sum(OrderItem.total_price), 0.0),
            )
            .join(Order, OrderItem.order_id == Order.id)
            .filter(Order.status.in_(REVENUE_STATUSES), OrderItem.product_id.in_(product_ids))
        )
        rev_query = _apply_date_range(rev_query, Order.created_at, start=start, end=end)
        revenue_by_product: dict[int, dict[str, float]] = {}
        for product_id, currency, revenue in rev_query.group_by(
            OrderItem.product_id, Order.currency
        ).all():
            if revenue:
                revenue_by_product.setdefault(product_id, {})[currency] = round(float(revenue), 2)

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
                    "units": int(units or 0),
                    "order_count": int(orders or 0),
                    "revenue_by_currency": revenue_by_product.get(product_id, {}),
                }
            )
        return result

    # ------------------------------------------------------------------
    # Revenue by category
    # ------------------------------------------------------------------

    def revenue_by_category(
        self,
        db: Session,
        *,
        start: date | None = None,
        end: date | None = None,
    ) -> list[dict]:
        """Per-category units + per-currency revenue over revenue-status orders.

        Attribution follows the product's **current** ``category_id`` (orders
        snapshot only the line name/sku, not the category). Products with no
        category (or whose product row was deleted) bucket under ``category_id =
        null`` / ``name = "Uncategorized"``. Ranked by total units desc; ties are
        broken with the uncategorized bucket last, then by category_id asc.
        """
        # LEFT join Product so deleted products still contribute (→ uncategorized).
        rows = (
            db.query(
                Product.category_id,
                func.coalesce(func.sum(OrderItem.quantity), 0).label("units"),
                Order.currency,
                func.coalesce(func.sum(OrderItem.total_price), 0.0).label("revenue"),
            )
            .select_from(OrderItem)
            .join(Order, OrderItem.order_id == Order.id)
            .outerjoin(Product, OrderItem.product_id == Product.id)
            .filter(Order.status.in_(REVENUE_STATUSES))
        )
        rows = _apply_date_range(rows, Order.created_at, start=start, end=end)
        grouped = rows.group_by(Product.category_id, Order.currency).all()
        if not grouped:
            return []

        per_category: dict[int | None, dict] = {}
        for category_id, units, currency, revenue in grouped:
            entry = per_category.setdefault(category_id, {"units": 0, "revenue_by_currency": {}})
            entry["units"] += int(units or 0)
            if revenue:
                existing = entry["revenue_by_currency"].get(currency, 0.0)
                entry["revenue_by_currency"][currency] = round(existing + float(revenue), 2)

        # Resolve current category names in one query.
        category_ids = [cid for cid in per_category if cid is not None]
        names = {
            c.id: c.name
            for c in db.query(Category.id, Category.name).filter(Category.id.in_(category_ids))
        }

        result: list[dict] = []
        for category_id, entry in per_category.items():
            result.append(
                {
                    "category_id": category_id,
                    "name": names.get(category_id, UNCATEGORIZED_NAME)
                    if category_id is not None
                    else UNCATEGORIZED_NAME,
                    "units": entry["units"],
                    "revenue_by_currency": entry["revenue_by_currency"],
                }
            )
        # Units desc; uncategorized (None) sinks to the bottom; stable by id asc.
        result.sort(
            key=lambda r: (
                -r["units"],
                r["category_id"] is None,
                r["category_id"] if r["category_id"] is not None else 0,
            )
        )
        return result

    # ------------------------------------------------------------------
    # Refund summary
    # ------------------------------------------------------------------

    def refund_summary(
        self,
        db: Session,
        *,
        start: date | None = None,
        end: date | None = None,
    ) -> dict:
        """Per-currency refunded total + count + rate over the inclusive range.

        ``refunded`` sums ``refunds.amount`` per currency; ``revenue`` is the
        comparable revenue-status order total in the same currency; ``rate`` is
        ``refunded / revenue`` (0.0 when no revenue). Refunds are attributed by
        ``Refund.created_at`` and revenue by ``Order.created_at`` — both bounded
        by the same range. Returns ``{"refund_count": int, "rows": [...]}``.
        """
        refund_q = db.query(
            Refund.currency,
            func.coalesce(func.sum(Refund.amount), 0.0),
            func.count(Refund.id),
        )
        refund_q = _apply_date_range(refund_q, Refund.created_at, start=start, end=end)
        refunds_by_currency: dict[str, dict] = {}
        total_count = 0
        for currency, amount, count in refund_q.group_by(Refund.currency).all():
            total_count += int(count)
            refunds_by_currency[currency] = {
                "refunded": round(float(amount or 0.0), 2),
                "count": int(count),
            }

        # Comparable revenue (revenue-status orders) per currency over the range.
        rev_q = db.query(
            Order.currency,
            func.coalesce(func.sum(Order.total), 0.0),
        ).filter(Order.status.in_(REVENUE_STATUSES))
        rev_q = _apply_date_range(rev_q, Order.created_at, start=start, end=end)
        revenue_by_currency = {
            currency: round(float(total or 0.0), 2)
            for currency, total in rev_q.group_by(Order.currency).all()
        }

        currencies = sorted(set(refunds_by_currency) | set(revenue_by_currency))
        rows: list[dict] = []
        for currency in currencies:
            refunded = refunds_by_currency.get(currency, {}).get("refunded", 0.0)
            count = refunds_by_currency.get(currency, {}).get("count", 0)
            revenue = revenue_by_currency.get(currency, 0.0)
            rate = round(refunded / revenue, 4) if revenue else 0.0
            rows.append(
                {
                    "currency": currency,
                    "refunded": refunded,
                    "refund_count": count,
                    "revenue": revenue,
                    "rate": rate,
                }
            )
        return {"refund_count": total_count, "rows": rows}


report = CRUDReport()
