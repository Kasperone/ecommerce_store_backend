"""Transactional return-request emails (requested / approved / rejected).

Mirrors ``app/services/order_email.py``: composed off the RMA lifecycle and
sent **best-effort** inside a ``BackgroundTask`` so a Resend hiccup never fails
the customer's request or an admin decision. Localized to the requester's
``User.locale`` (EN/PL, fallback "en") — returns are authed-owner-only in v1
(guest orders are excluded), so the recipient is always the order's user.

The ``refunded`` outcome reuses the existing *order* ``refunded`` email (KOR
attached) fired by the refund-from-return route — no return-specific refunded
copy here.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from app.core import email
from app.core.config import settings
from app.core.database import SessionLocal
from app.crud import return_request as crud_return_request
from app.services.brand import get_brand_name

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from app.models.return_request import ReturnRequest

logger = logging.getLogger(__name__)

# Per-locale copy. ``subject`` interpolates {name} (store) / {num} (order
# number); ``intro`` is a plain sentence wrapped in <p> and followed by the
# "view order" link. Fallback is "en" for any unknown locale.
_EVENT_TEXT: dict[str, dict[str, dict[str, str]]] = {
    "en": {
        "requested": {
            "subject": "{name} — Return request received for order {num}",
            "heading": "We've received your return request",
            "intro": "Thanks — your return request has been received and is awaiting "
            "review. We'll let you know as soon as it's been processed.",
        },
        "approved": {
            "subject": "{name} — Return approved for order {num}",
            "heading": "Your return has been approved",
            "intro": "Good news — your return request has been approved. Please send "
            "the items back to us; once they arrive we'll process your refund.",
        },
        "rejected": {
            "subject": "{name} — Return request declined for order {num}",
            "heading": "Your return request was declined",
            "intro": "Unfortunately we couldn't accept your return request. "
            "If you believe this is a mistake, please contact us.",
        },
    },
    "pl": {
        "requested": {
            "subject": "{name} — Otrzymano zgłoszenie zwrotu do zamówienia {num}",
            "heading": "Otrzymaliśmy Twoje zgłoszenie zwrotu",
            "intro": "Dziękujemy — Twoje zgłoszenie zwrotu zostało przyjęte i oczekuje "
            "na rozpatrzenie. Poinformujemy Cię, gdy tylko zostanie przetworzone.",
        },
        "approved": {
            "subject": "{name} — Zwrot do zamówienia {num} zaakceptowany",
            "heading": "Twój zwrot został zaakceptowany",
            "intro": "Dobra wiadomość — Twoje zgłoszenie zwrotu zostało zaakceptowane. "
            "Prosimy o odesłanie produktów; po ich otrzymaniu zrealizujemy zwrot środków.",
        },
        "rejected": {
            "subject": "{name} — Zgłoszenie zwrotu do zamówienia {num} odrzucone",
            "heading": "Twoje zgłoszenie zwrotu zostało odrzucone",
            "intro": "Niestety nie mogliśmy zaakceptować Twojego zgłoszenia zwrotu. "
            "Jeśli uważasz, że to pomyłka, skontaktuj się z nami.",
        },
    },
}

_VIEW_LABEL = {"en": "View your order", "pl": "Zobacz swoje zamówienie"}


def _texts_for(lang: str) -> dict[str, dict[str, str]]:
    return _EVENT_TEXT.get(lang, _EVENT_TEXT["en"])


def send_return_event(db: Session, ret: ReturnRequest, event: str) -> None:
    """Compose + send one return email for a loaded return request (best-effort).

    ``ret.order`` and ``ret.user`` must be reachable (eager-loaded by the CRUD
    reads, or lazy-loadable while the session is open). Swallows + logs any
    failure — a transactional email must never bubble into the caller's
    request or transaction. ``db`` is unused today but kept for signature
    parity with ``order_email.send_order_event`` (future attachments).
    """
    del db  # signature parity with order_email; no DB reads needed yet
    user = ret.user
    if user is None:
        # SET NULL requester (deleted account) — nobody to notify.
        logger.warning("Return %s has no requester; skipping %s email", ret.id, event)
        return
    lang = user.locale or "en"
    entry = _texts_for(lang).get(event)
    if entry is None:
        logger.warning("Unknown return-email event %r (return %s)", event, ret.id)
        return
    order = ret.order
    order_number = order.order_number if order is not None else f"#{ret.order_id}"
    view_label = _VIEW_LABEL.get(lang, _VIEW_LABEL["en"])
    view = (
        f'<p style="margin:16px 0;"><a href="{settings.FRONTEND_URL}/account/orders/'
        f'{ret.order_id}" style="color:#2563eb;">{view_label}</a></p>'
    )
    subject = entry["subject"].format(name=get_brand_name(), num=order_number)
    html = f'<h2 style="margin:0 0 12px;">{entry["heading"]}</h2>' f"<p>{entry['intro']}</p>{view}"
    try:
        email.send_email(
            email_to=user.email,
            subject=subject,
            html_content=html,
        )
    except Exception:
        logger.warning("Return email failed: event=%s return=%s", event, ret.id, exc_info=True)


def send_return_event_in_background(return_id: int, event: str) -> None:
    """BackgroundTask entry: open a fresh session, load the return, send.

    Runs after the response is sent, so the request's session is gone — own
    one here and close it (mirrors ``order_email.send_order_event_in_background``).
    """
    db = SessionLocal()
    try:
        ret = crud_return_request.get(db, return_id=return_id)
        if ret is None:
            logger.warning("Return %s not found for %s email", return_id, event)
            return
        send_return_event(db, ret, event)
    finally:
        db.close()
