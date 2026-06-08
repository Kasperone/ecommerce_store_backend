"""Low-stock report email (the manual inventory digest).

There is **no scheduler** in this app (no celery/apscheduler/cron — only
FastAPI in-request ``BackgroundTasks``), so the low-stock digest cannot
self-schedule. It is fired on demand by ``POST /admin/inventory/low-stock-report/
email`` and dispatched **best-effort** inside a ``BackgroundTask`` so a Resend
hiccup never fails the admin's request. An external/OS cron can hit that endpoint
with an admin token for periodic delivery.

The report is a single English email to the **store admin address**
(``store_config.email`` → ``settings.STORE_EMAIL`` → ``settings.EMAIL_FROM``),
listing every product currently at/below its effective low-stock threshold
(``crud_inventory.low_stock_products``, which honours the global default).
Mirrors ``app/services/order_email.py`` / ``return_email.py``: a thin compose +
best-effort send, plus a background entry that owns its own session.
"""

from __future__ import annotations

import logging
from html import escape as html_escape
from typing import TYPE_CHECKING

from app.core import email
from app.core.config import settings
from app.core.database import SessionLocal
from app.crud import store_config as crud_store_config
from app.models.product import Product

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def resolve_report_recipients(db: Session) -> list[str]:
    """The store admin address(es) the low-stock report goes to.

    Single recipient today: the editable ``store_config.email`` if set, else the
    ``STORE_EMAIL`` env value, else the ``EMAIL_FROM`` sender (always populated).
    Returned as a list so the route can surface it and a future multi-recipient
    config is a drop-in. Empty only in the pathological case of every source
    being blank.
    """
    config = crud_store_config.get_singleton(db)
    candidate = (config.email or "").strip() or (settings.STORE_EMAIL or "").strip()
    if not candidate:
        candidate = (settings.EMAIL_FROM or "").strip()
    return [candidate] if candidate else []


def _report_html(products: list[Product]) -> str:
    """The low-stock report body (a simple HTML table; free-text cells escaped)."""
    heading = '<h2 style="margin:0 0 12px;">Low-stock report</h2>'
    if not products:
        return (
            heading + "<p>No products are currently at or below their low-stock threshold. "
            "Nothing to action.</p>"
        )
    intro = (
        f"<p>{len(products)} product(s) are at or below their low-stock threshold "
        "and may need restocking:</p>"
    )
    rows = "".join(
        "<tr>"
        f'<td style="padding:4px 12px 4px 0;">{html_escape(p.name)}</td>'
        f'<td style="padding:4px 12px 4px 0;">{html_escape(p.sku or "—")}</td>'
        f'<td style="padding:4px 12px 4px 0;text-align:right;">{p.stock}</td>'
        f'<td style="padding:4px 0;text-align:right;">'
        f'{p.low_stock_threshold if p.low_stock_threshold is not None else "—"}</td>'
        "</tr>"
        for p in products
    )
    table = (
        '<table style="border-collapse:collapse;font-size:14px;">'
        "<thead><tr>"
        '<th style="text-align:left;padding:4px 12px 4px 0;">Product</th>'
        '<th style="text-align:left;padding:4px 12px 4px 0;">SKU</th>'
        '<th style="text-align:right;padding:4px 12px 4px 0;">Stock</th>'
        '<th style="text-align:right;padding:4px 0;">Threshold</th>'
        "</tr></thead>"
        f"<tbody>{rows}</tbody></table>"
    )
    return heading + intro + table


def send_low_stock_report(db: Session, recipients: list[str], products: list[Product]) -> None:
    """Compose + send the low-stock report to each recipient (best-effort).

    Swallows + logs any Resend failure per recipient — a transactional email must
    never bubble into the caller's request or transaction (mirrors the order/
    campaign best-effort posture).
    """
    subject = f"{settings.EMAIL_FROM_NAME} — Low-stock report ({len(products)} product(s))"
    html = _report_html(products)
    for recipient in recipients:
        try:
            email.send_email(email_to=recipient, subject=subject, html_content=html)
        except Exception:
            logger.warning("Low-stock report email failed: recipient=%s", recipient, exc_info=True)


def send_low_stock_report_in_background(recipients: list[str], product_ids: list[int]) -> None:
    """BackgroundTask entry: open a fresh session, load the products by id, send.

    Runs after the response is sent, so the request's session is gone — own one
    here and close it (mirrors ``order_email.send_order_event_in_background``).
    The recipients + the low-stock product **ids** are resolved in the request
    (and the count is what the response + audit row reported); the task re-loads
    those products fresh and sends. Loading by id (committed before the task runs)
    keeps the snapshot consistent with what the admin was told.
    """
    if not recipients:
        logger.warning("Low-stock report: no recipient configured; skipping send")
        return
    db = SessionLocal()
    try:
        products = (
            db.query(Product).filter(Product.id.in_(product_ids)).all() if product_ids else []
        )
        # Preserve the by-name order the report set used.
        products.sort(key=lambda p: (p.name, p.id))
        send_low_stock_report(db, recipients, products)
    finally:
        db.close()
