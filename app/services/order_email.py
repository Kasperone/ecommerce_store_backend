"""Transactional order emails (confirmation + status changes).

Composed off the order lifecycle and sent **best-effort** inside a
``BackgroundTask`` so a Resend hiccup never fails checkout or an admin status
update. The confirmation + paid events attach the invoice PDF (built via the
invoice service). Localized to the recipient's ``User.locale`` (EN/PL) — the
language is a stored preference because the send happens off the request (webhook
/ admin action), where there is no customer UI locale to read.
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

# Events that attach the order document: confirmation rides a *proforma* (the order
# is still pending), paid rides the numbered *faktura* (see render_invoice_pdf).
_ATTACH_EVENTS = {"confirmation", "paid"}

# Per-locale email copy. ``subject`` interpolates {name}/{num}; ``intro`` is a plain
# sentence the builder wraps in <p> and follows with the "view order" link. Fallback
# is "en" for any unknown locale.
_EVENT_TEXT: dict[str, dict[str, dict[str, str]]] = {
    "en": {
        "confirmation": {
            "subject": "{name} — Order {num} confirmed",
            "heading": "Thanks for your order!",
            "intro": "We've received your order and it's now being processed. "
            "Your proforma invoice is attached.",
        },
        "paid": {
            "subject": "{name} — Payment received for order {num}",
            "heading": "Payment received",
            "intro": "We've received your payment — thank you! Your paid invoice is attached.",
        },
        "shipped": {
            "subject": "{name} — Order {num} shipped",
            "heading": "Your order is on its way",
            "intro": "Good news — your order has shipped.",
        },
        "delivered": {
            "subject": "{name} — Order {num} delivered",
            "heading": "Your order has been delivered",
            "intro": "Your order has been marked delivered. We hope you enjoy it!",
        },
        "cancelled": {
            "subject": "{name} — Order {num} cancelled",
            "heading": "Your order was cancelled",
            "intro": "Your order has been cancelled and any reserved stock released. "
            "If this was unexpected, please contact us.",
        },
    },
    "pl": {
        "confirmation": {
            "subject": "{name} — Zamówienie {num} potwierdzone",
            "heading": "Dziękujemy za zamówienie!",
            "intro": "Otrzymaliśmy Twoje zamówienie i jest ono realizowane. "
            "Faktura proforma w załączniku.",
        },
        "paid": {
            "subject": "{name} — Otrzymano płatność za zamówienie {num}",
            "heading": "Płatność otrzymana",
            "intro": "Otrzymaliśmy Twoją płatność — dziękujemy! Opłacona faktura w załączniku.",
        },
        "shipped": {
            "subject": "{name} — Zamówienie {num} wysłane",
            "heading": "Twoje zamówienie jest w drodze",
            "intro": "Dobra wiadomość — Twoje zamówienie zostało wysłane.",
        },
        "delivered": {
            "subject": "{name} — Zamówienie {num} dostarczone",
            "heading": "Twoje zamówienie zostało dostarczone",
            "intro": "Twoje zamówienie zostało oznaczone jako dostarczone. "
            "Mamy nadzieję, że spełni Twoje oczekiwania!",
        },
        "cancelled": {
            "subject": "{name} — Zamówienie {num} anulowane",
            "heading": "Twoje zamówienie zostało anulowane",
            "intro": "Twoje zamówienie zostało anulowane, a zarezerwowany towar zwolniony. "
            "Jeśli to pomyłka, skontaktuj się z nami.",
        },
    },
}

_VIEW_LABEL = {"en": "View your order", "pl": "Zobacz swoje zamówienie"}


def _texts_for(lang: str) -> dict[str, dict[str, str]]:
    return _EVENT_TEXT.get(lang, _EVENT_TEXT["en"])


def event_for_status(status: str) -> str | None:
    """The email event for a status the admin PATCH transitions into (or None)."""
    return _STATUS_EVENTS.get(status)


def _spec_for(event: str, order: Order, lang: str) -> _EventSpec | None:
    entry = _texts_for(lang).get(event)
    if entry is None:
        return None
    view_label = _VIEW_LABEL.get(lang, _VIEW_LABEL["en"])
    view = (
        f'<p style="margin:16px 0;"><a href="{settings.FRONTEND_URL}/account/orders/{order.id}" '
        f'style="color:#2563eb;">{view_label}</a></p>'
    )
    return _EventSpec(
        subject=entry["subject"].format(name=settings.EMAIL_FROM_NAME, num=order.order_number),
        heading=entry["heading"],
        intro_html=f"<p>{entry['intro']}</p>{view}",
        attach_invoice=event in _ATTACH_EVENTS,
    )


def send_order_event(db: Session, order: Order, event: str) -> None:
    """Compose + send one order email for a loaded order (best-effort).

    ``db`` is needed to resolve the invoice document: a paid order's confirmation/
    paid email carries its numbered faktura, while a still-pending confirmation
    carries a proforma (``render_invoice_pdf`` decides from the registry). The email
    + attached document render in the recipient's ``User.locale`` (fallback "en").
    Swallows + logs any failure — a transactional email must never bubble into the
    caller's request or transaction.
    """
    lang = (order.user.locale if order.user else None) or "en"
    spec = _spec_for(event, order, lang)
    if spec is None:
        logger.warning("Unknown order-email event %r (order %s)", event, order.order_number)
        return
    try:
        invoice_pdf = (
            render_invoice_pdf(db, order, store=StoreIdentity.from_settings(settings), lang=lang)
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
            lang=lang,
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
