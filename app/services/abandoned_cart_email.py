"""Abandoned-cart recovery emails (the one-shot detection + send pass).

There is **no scheduler** in this app (no celery/apscheduler/cron — only
FastAPI in-request ``BackgroundTasks``), so detection runs as an on-demand
entry point driven by external OS cron:
``python -m app.scripts.send_abandoned_cart_emails`` (see the script module).

One localized (EN/PL) email per abandoned cart, to **authenticated users
only** (guests never reach the backend before order creation — v1 scope), and
only when the user's latest ``marketing_email`` consent is **explicitly
granted** (append-only ``user_consents`` log, latest row wins; unset or
revoked = no send — PL law, UŚUDE, requires prior consent). The CTA link
carries an opaque 256-bit token that restores cart contents only — it never
authenticates.

Mirrors ``order_email.py`` / ``inventory_email.py``: per-locale copy dict,
thin compose, best-effort send (one Resend failure skips that user, never
aborts the run), and a commit **per email** so partial progress survives a
crash and a token is only live if its email actually went out.
"""

from __future__ import annotations

import logging
import secrets
from datetime import UTC, datetime, timedelta
from html import escape as html_escape
from typing import TYPE_CHECKING

from app.core import email
from app.core.config import settings
from app.crud import saved_cart as crud_saved_cart
from app.crud import store_config as crud_store_config
from app.crud import user_consent as crud_user_consent
from app.models.product import Product
from app.services.brand import get_email_brand_name

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from app.models.saved_cart import SavedCart

logger = logging.getLogger(__name__)

# Per-locale copy. ``subject`` interpolates {name} (the brand); fallback "en"
# for any unknown locale, like order_email.py.
_TEXT: dict[str, dict[str, str]] = {
    "en": {
        "subject": "{name} — You left something in your cart",
        "heading": "Still thinking it over?",
        "greeting": "Hi {first_name},",
        "intro": "The items below are still waiting in your cart — "
        "one click brings them right back.",
        "cta": "Restore my cart",
        "footer": "You received this email because you have an account with us. "
        '<a href="{prefs_url}" style="color:#2563eb;">Manage email preferences</a>',
    },
    "pl": {
        "subject": "{name} — Twój koszyk czeka",
        "heading": "Dokończ zakupy",
        "greeting": "Cześć {first_name},",
        "intro": "Poniższe produkty wciąż czekają w Twoim koszyku — "
        "jedno kliknięcie przywróci je z powrotem.",
        "cta": "Przywróć koszyk",
        "footer": "Otrzymujesz tę wiadomość, ponieważ masz u nas konto. "
        '<a href="{prefs_url}" style="color:#2563eb;">Zarządzaj preferencjami</a>',
    },
}


def _texts_for(lang: str) -> dict[str, str]:
    return _TEXT.get(lang, _TEXT["en"])


def _locale_prefix(lang: str) -> str:
    """URL locale prefix matching the frontend's i18n ``prefix_except_default``."""
    return "/pl" if lang == "pl" else ""


def _item_lines(db: Session, cart: SavedCart, lang: str) -> list[str]:
    """Human-readable "Name x qty" lines for the cart's live products.

    Localizes with ``name_pl or name`` for ``pl``; lines whose product is
    gone/inactive are dropped (same posture as the recovery endpoint). All
    user/product strings are HTML-escaped by the caller's builder.
    """
    lines: list[str] = []
    for entry in cart.items or []:
        product = db.get(Product, entry.get("product_id"))
        if product is None or not product.is_active:
            continue
        name = (product.name_pl or product.name) if lang == "pl" else product.name
        lines.append(f"{name} x {entry.get('quantity', 1)}")
    return lines


def _build_html(cart: SavedCart, lang: str, token: str, item_lines: list[str]) -> str:
    """The recovery email body (heading, item list, CTA button, prefs footer)."""
    texts = _texts_for(lang)
    prefix = _locale_prefix(lang)
    recover_url = f"{settings.FRONTEND_URL}{prefix}/cart/recover/{token}"
    prefs_url = f"{settings.FRONTEND_URL}{prefix}/account"
    first_name = cart.user.first_name or ""
    items_html = "".join(
        f'<li style="margin:4px 0;">{html_escape(line)}</li>' for line in item_lines
    )
    return (
        f'<h2 style="margin:0 0 12px;">{texts["heading"]}</h2>'
        f"<p>{html_escape(texts['greeting'].format(first_name=first_name))}</p>"
        f"<p>{texts['intro']}</p>"
        f'<ul style="padding-left:20px;">{items_html}</ul>'
        f'<p style="margin:24px 0;"><a href="{recover_url}" '
        'style="background:#2563eb;color:#ffffff;padding:10px 20px;'
        f'border-radius:6px;text-decoration:none;">{texts["cta"]}</a></p>'
        f'<p style="margin:24px 0 0;font-size:12px;color:#6b7280;">'
        f"{texts['footer'].format(prefs_url=prefs_url)}</p>"
    )


def send_abandoned_cart_emails(db: Session) -> int:
    """Detect abandoned carts and send each owner their one recovery email.

    Returns the send count. Disabled config → 0 (healthy no-op). Consent gate:
    only users whose latest ``marketing_email`` consent is explicitly granted
    are emailed (unset or revoked = skip). Each successful send stamps the
    token + sent-at and commits immediately, so a crash mid-run never leaves a
    live token without its email nor re-sends already-notified carts.
    """
    config = crud_store_config.get_singleton(db)
    if not config.abandoned_cart_enabled:
        logger.info("Abandoned-cart recovery disabled (store_config); nothing to do")
        return 0

    cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(
        hours=config.abandoned_cart_threshold_hours
    )
    candidates = crud_saved_cart.abandoned_candidates(db, cutoff=cutoff)
    sent = 0
    for cart in candidates:
        user = cart.user
        consents = crud_user_consent.current_map(db, user_id=user.id)
        if consents.get("marketing_email") is not True:
            # Explicit opt-in only (UŚUDE): unset or revoked = never send.
            continue
        lang = user.locale or "en"
        item_lines = _item_lines(db, cart, lang)
        if not item_lines:
            # Every line is dead (product gone/inactive) — nothing to recover.
            continue
        token = secrets.token_urlsafe(32)
        try:
            email.send_email(
                email_to=user.email,
                subject=_texts_for(lang)["subject"].format(name=get_email_brand_name()),
                html_content=_build_html(cart, lang, token, item_lines),
            )
        except Exception:
            # Best-effort per user — one Resend failure never aborts the run.
            logger.warning(
                "Abandoned-cart email failed: user=%s cart=%s", user.id, cart.id, exc_info=True
            )
            continue
        cart.recovery_token = token
        cart.recovery_email_sent_at = datetime.now(UTC).replace(tzinfo=None)
        db.add(cart)
        db.commit()
        sent += 1
    logger.info("Abandoned-cart run: %d candidate(s), %d email(s) sent", len(candidates), sent)
    return sent
