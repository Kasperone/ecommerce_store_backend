"""B2B net-terms dunning — overdue pay-on-account reminder emails (tail Program B).

A net-terms order ships now and is paid later by bank transfer (Program 2). When
the printed ``termin płatności`` (``orders.payment_due_at``) passes and the
merchant hasn't recorded the transfer (``net_terms_paid_at`` still NULL), the
buyer gets a polite overdue reminder.

Like ``abandoned_cart_email.py`` there is **no scheduler** — an OS-cron entry
point (``app.scripts.send_net_terms_reminders``) drives this one-shot detection
pass. Unlike abandoned-cart, these are **transactional** (about an existing
invoice debt, not marketing), so there is no ``marketing_email`` consent gate;
the feature is instead dark by default behind ``settings.NET_TERMS_DUNNING_ENABLED``.

Re-send is throttled to at most once per ``NET_TERMS_DUNNING_INTERVAL_DAYS`` per
order, deduped via a ``net_terms_reminder`` **order_event** (the timeline table
already exists — no schema change). Each successful send records its event and
commits immediately, so a crash mid-run never re-sends an already-reminded order.
Best-effort per order: one Resend failure is logged and skipped, never fatal.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from html import escape as html_escape
from typing import TYPE_CHECKING

from app.core import email
from app.core.config import settings
from app.crud import order_event as crud_order_event
from app.models.order import Order, OrderStatus
from app.models.order_event import OrderEvent
from app.services.b2b_net_terms import NET_TERMS_METHOD
from app.services.brand import get_email_brand_name

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

REMINDER_EVENT_KIND = "net_terms_reminder"

# Per-locale copy. ``subject`` interpolates {name} (brand) + {num} (order number);
# fallback "en" for any unknown locale (mirrors order_email.py).
_TEXT: dict[str, dict[str, str]] = {
    "en": {
        "subject": "{name} — Invoice for order {num} is overdue",
        "heading": "Payment overdue",
        "greeting": "Hi {first_name},",
        "intro": "Our records show the invoice for order <strong>{num}</strong> "
        "was due on <strong>{due}</strong> and is still outstanding.",
        "amount": "Amount due: <strong>{amount}</strong>",
        "cta": "Please settle it by bank transfer at your earliest convenience. "
        "If you've already paid, thank you — kindly disregard this reminder.",
        "footer": "You're receiving this because your company pays this order on account.",
    },
    "pl": {
        "subject": "{name} — Faktura za zamówienie {num} jest przeterminowana",
        "heading": "Zaległa płatność",
        "greeting": "Dzień dobry {first_name},",
        "intro": "Z naszych danych wynika, że faktura za zamówienie <strong>{num}</strong> "
        "miała termin płatności <strong>{due}</strong> i wciąż pozostaje nieopłacona.",
        "amount": "Kwota do zapłaty: <strong>{amount}</strong>",
        "cta": "Prosimy o uregulowanie należności przelewem w najbliższym możliwym terminie. "
        "Jeśli płatność została już wykonana — dziękujemy i prosimy zignorować tę wiadomość.",
        "footer": "Otrzymujesz tę wiadomość, ponieważ Twoja firma płaci za to zamówienie na konto.",
    },
}


def _texts_for(lang: str) -> dict[str, str]:
    return _TEXT.get(lang, _TEXT["en"])


def _build_html(order: Order, lang: str, first_name: str) -> str:
    texts = _texts_for(lang)
    num = html_escape(order.order_number)
    due = order.payment_due_at.date().isoformat() if order.payment_due_at else ""
    amount = html_escape(f"{order.total} {order.currency}")
    return (
        f'<h2 style="margin:0 0 12px;">{texts["heading"]}</h2>'
        f"<p>{html_escape(texts['greeting'].format(first_name=first_name))}</p>"
        f"<p>{texts['intro'].format(num=num, due=html_escape(due))}</p>"
        f"<p>{texts['amount'].format(amount=amount)}</p>"
        f"<p>{texts['cta']}</p>"
        f'<p style="margin:24px 0 0;font-size:12px;color:#6b7280;">{texts["footer"]}</p>'
    )


def _last_reminder_at(db: Session, order_id: int) -> datetime | None:
    """The ``created_at`` of the most recent reminder event for ``order_id`` (or None)."""
    return (
        db.query(OrderEvent.created_at)
        .filter(OrderEvent.order_id == order_id, OrderEvent.kind == REMINDER_EVENT_KIND)
        .order_by(OrderEvent.created_at.desc())
        .limit(1)
        .scalar()
    )


def send_net_terms_reminders(db: Session) -> int:
    """Detect overdue unpaid net-terms orders and send each buyer a reminder.

    Returns the send count. Disabled config → 0 (healthy no-op). Throttled to one
    email per order per ``NET_TERMS_DUNNING_INTERVAL_DAYS`` via a
    ``net_terms_reminder`` order_event. Commits per email so partial progress
    survives a crash and no order is reminded twice within the window.
    """
    if not settings.NET_TERMS_DUNNING_ENABLED:
        logger.info("Net-terms dunning disabled (settings); nothing to do")
        return 0

    now = datetime.now(UTC).replace(tzinfo=None)
    interval = timedelta(days=settings.NET_TERMS_DUNNING_INTERVAL_DAYS)
    overdue = (
        db.query(Order)
        .filter(
            Order.payment_method == NET_TERMS_METHOD,
            Order.net_terms_paid_at.is_(None),
            Order.status != OrderStatus.CANCELLED,
            Order.payment_due_at.isnot(None),
            Order.payment_due_at < now,
        )
        .all()
    )

    sent = 0
    for order in overdue:
        last = _last_reminder_at(db, order.id)
        if last is not None and (now - last) < interval:
            continue  # already reminded within the throttle window
        email_to = order.user.email if order.user else order.guest_email
        if email_to is None:
            continue  # defensive: the CHECK guarantees a recipient exists
        lang = (order.user.locale if order.user else order.guest_locale) or "en"
        first_name = (order.user.first_name if order.user else "") or ""
        try:
            email.send_email(
                email_to=email_to,
                subject=_texts_for(lang)["subject"].format(
                    name=get_email_brand_name(), num=order.order_number
                ),
                html_content=_build_html(order, lang, first_name),
            )
        except Exception:
            # Best-effort per order — one Resend failure never aborts the run.
            logger.warning("Net-terms reminder failed: order=%s", order.order_number, exc_info=True)
            continue
        overdue_days = (now - order.payment_due_at).days if order.payment_due_at else None
        crud_order_event.record(
            db,
            order_id=order.id,
            kind=REMINDER_EVENT_KIND,
            payload={
                "due_at": order.payment_due_at.isoformat() if order.payment_due_at else None,
                "overdue_days": overdue_days,
            },
        )
        db.commit()
        sent += 1

    logger.info("Net-terms dunning run: %d overdue, %d reminder(s) sent", len(overdue), sent)
    return sent
