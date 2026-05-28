"""Transactional order emails (confirmation + status changes).

Composed off the order lifecycle and sent **best-effort** inside a
``BackgroundTask`` so a Resend hiccup never fails checkout or an admin status
update. The confirmation + paid events attach the invoice PDF (built via the
invoice service). English-only for now — ``User`` has no locale field yet.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.core import email
from app.core.config import settings
from app.core.database import SessionLocal
from app.crud import order as crud_order
from app.services.invoice import StoreIdentity, render_invoice_pdf

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from app.models.order import Order

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _EventSpec:
    subject: str
    heading: str
    intro_html: str
    attach_invoice: bool = False


# OrderStatus values that map 1:1 to a notification event on the admin status
# PATCH. Cancelled is fired by the cancel endpoint, confirmation by order create.
_STATUS_EVENTS = {"paid": "paid", "shipped": "shipped", "delivered": "delivered"}


def event_for_status(status: str) -> str | None:
    """The email event for a status the admin PATCH transitions into (or None)."""
    return _STATUS_EVENTS.get(status)


def _spec_for(event: str, order: Order) -> _EventSpec | None:
    name = settings.EMAIL_FROM_NAME
    num = order.order_number
    view = (
        f'<p style="margin:16px 0;"><a href="{settings.FRONTEND_URL}/account/orders/{order.id}" '
        f'style="color:#2563eb;">View your order</a></p>'
    )
    specs: dict[str, _EventSpec] = {
        "confirmation": _EventSpec(
            subject=f"{name} — Order {num} confirmed",
            heading="Thanks for your order!",
            intro_html=f"<p>We've received your order and it's now being processed. Your invoice is attached.</p>{view}",
            attach_invoice=True,
        ),
        "paid": _EventSpec(
            subject=f"{name} — Payment received for order {num}",
            heading="Payment received",
            intro_html=f"<p>We've received your payment — thank you! Your paid invoice is attached.</p>{view}",
            attach_invoice=True,
        ),
        "shipped": _EventSpec(
            subject=f"{name} — Order {num} shipped",
            heading="Your order is on its way",
            intro_html=f"<p>Good news — your order has shipped.</p>{view}",
        ),
        "delivered": _EventSpec(
            subject=f"{name} — Order {num} delivered",
            heading="Your order has been delivered",
            intro_html=f"<p>Your order has been marked delivered. We hope you enjoy it!</p>{view}",
        ),
        "cancelled": _EventSpec(
            subject=f"{name} — Order {num} cancelled",
            heading="Your order was cancelled",
            intro_html=f"<p>Your order has been cancelled and any reserved stock released. If this was unexpected, please contact us.</p>{view}",
        ),
    }
    return specs.get(event)


def send_order_event(db: Session, order: Order, event: str) -> None:
    """Compose + send one order email for a loaded order (best-effort).

    ``db`` is needed to resolve the invoice document: a paid order's confirmation/
    paid email carries its numbered faktura, while a still-pending confirmation
    carries a proforma (``render_invoice_pdf`` decides from the registry). Swallows
    + logs any failure — a transactional email must never bubble into the caller's
    request or transaction.
    """
    spec = _spec_for(event, order)
    if spec is None:
        logger.warning("Unknown order-email event %r (order %s)", event, order.order_number)
        return
    try:
        invoice_pdf = (
            render_invoice_pdf(db, order, store=StoreIdentity.from_settings(settings))
            if spec.attach_invoice
            else None
        )
        email.send_order_email(
            email_to=order.user.email,
            subject=spec.subject,
            heading=spec.heading,
            intro_html=spec.intro_html,
            order=order,
            invoice_pdf=invoice_pdf,
        )
    except Exception:
        logger.warning(
            "Order email failed: event=%s order=%s", event, order.order_number, exc_info=True
        )


def send_order_event_in_background(order_id: int, event: str) -> None:
    """BackgroundTask entry: open a fresh session, load the order, send.

    Runs after the response is sent, so the request's session is gone — own one
    here and close it. Lazy loads (items/user) resolve while it's open.
    """
    db = SessionLocal()
    try:
        order = crud_order.get(db, id=order_id)
        if order is None:
            logger.warning("Order %s not found for %s email", order_id, event)
            return
        send_order_event(db, order, event)
    finally:
        db.close()
