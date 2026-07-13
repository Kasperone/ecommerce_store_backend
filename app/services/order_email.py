"""Transactional order emails (confirmation + status changes).

Composed off the order lifecycle and sent **best-effort** inside a
``BackgroundTask`` so a Resend hiccup never fails checkout or an admin status
update. The confirmation + paid events attach the invoice PDF (built via the
invoice service). Localized to the recipient's ``User.locale`` (EN/PL) — the
language is a stored preference because the send happens off the request (webhook
/ admin action), where there is no customer UI locale to read. Guest orders
(``user_id`` NULL) send to ``order.guest_email`` in ``order.guest_locale``, and
their "view order" link points at the public token-tracking page.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.core import email
from app.core.config import settings
from app.core.database import SessionLocal
from app.crud import corrective_invoice as crud_corrective
from app.crud import order as crud_order
from app.services.brand import get_email_brand_name
from app.services.invoice import (
    StoreIdentity,
    render_corrective_invoice_pdf,
    render_invoice_pdf,
)

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
    attach_corrective: bool = False


# OrderStatus values that map 1:1 to a notification event on the admin status
# PATCH. Cancelled is fired by the cancel endpoint, confirmation by order create,
# refunded by the dedicated refund endpoint.
_STATUS_EVENTS = {"paid": "paid", "shipped": "shipped", "delivered": "delivered"}

# Events that attach the order document: confirmation rides a *proforma* (the order
# is still pending), paid rides the numbered *faktura* (see render_invoice_pdf).
_ATTACH_EVENTS = {"confirmation", "paid"}

# Events that attach the faktura korygująca (one event so far — the refund).
_ATTACH_CORRECTIVE_EVENTS = {"refunded"}

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
        "refunded": {
            "subject": "{name} — Refund issued for order {num}",
            "heading": "Your refund has been issued",
            "intro": "We've issued a refund for your order. Funds will be credited "
            "back to your original payment method within 5-10 business days. "
            "A corrective invoice (faktura korygująca) is attached.",
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
        "refunded": {
            "subject": "{name} — Zwrot za zamówienie {num}",
            "heading": "Zwrot został zarejestrowany",
            "intro": "Wystawiliśmy zwrot za Twoje zamówienie. Środki zostaną "
            "zwrócone tą samą metodą płatności w ciągu 5-10 dni roboczych. "
            "W załączniku faktura korygująca.",
        },
    },
}

_VIEW_LABEL = {"en": "View your order", "pl": "Zobacz swoje zamówienie"}

# Shipment-tracking labels for the shipped event (fallback "en", like the copy).
_TRACKING_LABELS = {
    "en": {"carrier": "Carrier", "number": "Tracking number", "track": "Track your shipment"},
    "pl": {"carrier": "Przewoźnik", "number": "Numer przesyłki", "track": "Śledź swoją przesyłkę"},
}


def _texts_for(lang: str) -> dict[str, dict[str, str]]:
    return _EVENT_TEXT.get(lang, _EVENT_TEXT["en"])


def _tracking_html(order: Order, lang: str) -> str:
    """Carrier / tracking-number / tracking-link block for the shipped email.

    Empty string when the order carries no tracking info (the email reads as
    before). The link renders only when ``tracking_url`` is set — there is no
    carrier→URL guessing.
    """
    if not (order.shipping_carrier or order.tracking_number or order.tracking_url):
        return ""
    labels = _TRACKING_LABELS.get(lang, _TRACKING_LABELS["en"])
    lines: list[str] = []
    if order.shipping_carrier:
        lines.append(f"{labels['carrier']}: {order.shipping_carrier}")
    if order.tracking_number:
        lines.append(f"{labels['number']}: {order.tracking_number}")
    html = f'<p style="margin:16px 0 0;">{"<br>".join(lines)}</p>' if lines else ""
    if order.tracking_url:
        html += (
            f'<p style="margin:8px 0 0;"><a href="{order.tracking_url}" '
            f'style="color:#2563eb;">{labels["track"]}</a></p>'
        )
    return html


def event_for_status(status: str) -> str | None:
    """The email event for a status the admin PATCH transitions into (or None)."""
    return _STATUS_EVENTS.get(status)


def _view_order_url(order: Order) -> str:
    """The customer-facing "view order" link.

    Authed orders point at the account order detail; guest orders (no account
    to log into) point at the public token-tracking page — the token in the
    email is itself the credential (proof-of-inbox).
    """
    if order.user_id is None and order.guest_token:
        return f"{settings.FRONTEND_URL}/orders/track/{order.guest_token}"
    return f"{settings.FRONTEND_URL}/account/orders/{order.id}"


def _spec_for(event: str, order: Order, lang: str) -> _EventSpec | None:
    entry = _texts_for(lang).get(event)
    if entry is None:
        return None
    view_label = _VIEW_LABEL.get(lang, _VIEW_LABEL["en"])
    view = (
        f'<p style="margin:16px 0;"><a href="{_view_order_url(order)}" '
        f'style="color:#2563eb;">{view_label}</a></p>'
    )
    # The shipped email carries the carrier + tracking number + tracking link
    # (when recorded on the order); other events never show tracking.
    tracking = _tracking_html(order, lang) if event == "shipped" else ""
    return _EventSpec(
        subject=entry["subject"].format(name=get_email_brand_name(), num=order.order_number),
        heading=entry["heading"],
        intro_html=f"<p>{entry['intro']}</p>{tracking}{view}",
        attach_invoice=event in _ATTACH_EVENTS,
        attach_corrective=event in _ATTACH_CORRECTIVE_EVENTS,
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
    # Guest orders carry their own contact email + language preference.
    lang = (order.user.locale if order.user else order.guest_locale) or "en"
    email_to = order.user.email if order.user else order.guest_email
    if email_to is None:
        # Defensive: the DB CHECK guarantees user_id OR guest_email, so this
        # only fires on a detached/partial instance — never silently drop.
        logger.warning("Order %s has no recipient email; skipping %s", order.order_number, event)
        return
    spec = _spec_for(event, order, lang)
    if spec is None:
        logger.warning("Unknown order-email event %r (order %s)", event, order.order_number)
        return
    try:
        store = StoreIdentity.from_store_config(db, settings)
        invoice_pdf = (
            render_invoice_pdf(db, order, store=store, lang=lang) if spec.attach_invoice else None
        )
        corrective_pdf: bytes | None = None
        corrective_number: str | None = None
        if spec.attach_corrective:
            # For the refunded event we attach the KOR for the most recent
            # refund on the order (v1 ships full refunds only, so there is at
            # most one). Order.refunds is ascending by created_at, so the
            # latest is the last entry; loaded by get_scoped's eager-load.
            latest_refund = order.refunds[-1] if order.refunds else None
            if latest_refund is not None:
                corrective_pdf = render_corrective_invoice_pdf(
                    db, latest_refund, store=store, lang=lang
                )
                kor = crud_corrective.get_by_refund_id(db, latest_refund.id)
                corrective_number = kor.kor_number if kor is not None else None
        email.send_order_email(
            email_to=email_to,
            subject=spec.subject,
            heading=spec.heading,
            intro_html=spec.intro_html,
            order=order,
            invoice_pdf=invoice_pdf,
            corrective_invoice_pdf=corrective_pdf,
            corrective_invoice_number=corrective_number,
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
