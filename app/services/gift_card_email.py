"""Gift-card issue email (EN/PL) — the one email that carries the plaintext code.

Mirrors ``abandoned_cart_email.py`` / ``order_email.py``: a per-locale copy
dict, a pure body builder (unit-testable without transport), and delivery via
``app.core.email.send_email`` (so the test suite's email mock captures it).
Brand name resolves through ``app/services/brand.py`` (admin-editable, fail-open
to the env value).

Per D4 there is no resend path — if this email is lost, the admin re-issues.
The route sends best-effort: the 201 response already carries the code, so a
transport failure must never fail the issue.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from html import escape as html_escape

from app.core import email
from app.services.brand import get_email_brand_name

# Per-locale copy. ``subject`` interpolates {name} (the brand); fallback "en"
# for any unknown locale, like order_email.py.
_TEXT: dict[str, dict[str, str]] = {
    "en": {
        "subject": "{name} — You've received a gift card",
        "heading": "You've received a gift card!",
        "intro": "Here is your gift card. Enter the code below at checkout to use it.",
        "code_label": "Your gift card code",
        "value_label": "Value",
        "expires_label": "Valid until",
        "message_label": "A message for you",
        "footer": "Keep this code safe — anyone with the code can use it.",
    },
    "pl": {
        "subject": "{name} — Otrzymujesz kartę podarunkową",
        "heading": "Otrzymujesz kartę podarunkową!",
        "intro": "Oto Twoja karta podarunkowa. Wpisz poniższy kod przy składaniu "
        "zamówienia, aby z niej skorzystać.",
        "code_label": "Kod Twojej karty podarunkowej",
        "value_label": "Wartość",
        "expires_label": "Ważna do",
        "message_label": "Wiadomość dla Ciebie",
        "footer": "Zachowaj ten kod w bezpiecznym miejscu — każdy, kto go zna, "
        "może z niego skorzystać.",
    },
}


def _texts_for(lang: str) -> dict[str, str]:
    return _TEXT.get(lang, _TEXT["en"])


def build_gift_card_email(
    *,
    code: str,
    amount: Decimal,
    currency: str,
    expires_at: datetime | None,
    message: str | None = None,
    lang: str = "en",
) -> tuple[str, str, str]:
    """Build ``(subject, html, text)`` for the issue email (pure — no transport).

    ``code`` arrives dash-formatted from the issue path; it is rendered in a
    monospace block so it survives copy/paste. The optional expiry renders as
    a plain date (the column is naive UTC). ``message`` is the buyer's personal
    gift note (purchased cards, v2); rendered escaped when present.
    """
    texts = _texts_for(lang)
    brand = get_email_brand_name()
    subject = texts["subject"].format(name=brand)
    value = f"{amount:,.2f} {currency}"
    expiry_html = ""
    expiry_text = ""
    if expires_at is not None:
        expiry = expires_at.strftime("%Y-%m-%d")
        expiry_html = (
            f'<p style="margin:4px 0;color:#6b7280;">{texts["expires_label"]}: '
            f"<strong>{expiry}</strong></p>"
        )
        expiry_text = f"{texts['expires_label']}: {expiry}\n"
    message_html = ""
    message_text = ""
    note = (message or "").strip()
    if note:
        message_html = (
            f'<div style="margin:24px 0;padding:16px 18px;background:#f9fafb;'
            f'border-left:3px solid #2563eb;border-radius:4px;">'
            f'<p style="margin:0 0 6px;color:#6b7280;font-size:13px;">'
            f'{html_escape(texts["message_label"])}</p>'
            f'<p style="margin:0;white-space:pre-wrap;">{html_escape(note)}</p></div>'
        )
        message_text = f"{texts['message_label']}: {note}\n\n"

    html_content = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>{html_escape(texts["heading"])}</title>
    </head>
    <body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
                 line-height:1.6;color:#333;max-width:600px;margin:0 auto;padding:20px;">
        <div style="background:#ffffff;border-radius:8px;padding:40px;
                    box-shadow:0 2px 4px rgba(0,0,0,0.1);">
            <h1 style="color:#2563eb;margin-top:0;font-size:24px;">
                {html_escape(texts["heading"])}</h1>
            <p>{html_escape(texts["intro"])}</p>
            {message_html}
            <p style="margin:24px 0 4px;color:#6b7280;">{texts["code_label"]}:</p>
            <p style="font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
                      font-size:22px;letter-spacing:2px;background:#f3f4f6;
                      border-radius:6px;padding:14px 18px;text-align:center;
                      margin:0 0 16px;">{html_escape(code)}</p>
            <p style="margin:4px 0;color:#6b7280;">{texts["value_label"]}:
                <strong>{value}</strong></p>
            {expiry_html}
            <div style="margin-top:40px;padding-top:20px;border-top:1px solid #e5e7eb;
                        font-size:13px;color:#6b7280;">
                <p style="margin:0 0 8px;">{html_escape(texts["footer"])}</p>
                &copy; {brand}
            </div>
        </div>
    </body>
    </html>
    """

    text_content = (
        f"{texts['heading']}\n\n"
        f"{texts['intro']}\n\n"
        f"{message_text}"
        f"{texts['code_label']}: {code}\n"
        f"{texts['value_label']}: {value}\n"
        f"{expiry_text}\n"
        f"{texts['footer']}\n\n"
        f"© {brand}\n"
    )
    return subject, html_content, text_content


def send_gift_card_email(
    *,
    email_to: str,
    code: str,
    amount: Decimal,
    currency: str,
    expires_at: datetime | None = None,
    message: str | None = None,
    lang: str = "en",
) -> dict:
    """Send the issue email carrying the plaintext ``code`` (EN/PL)."""
    subject, html_content, text_content = build_gift_card_email(
        code=code,
        amount=amount,
        currency=currency,
        expires_at=expires_at,
        message=message,
        lang=lang,
    )
    return email.send_email(
        email_to=email_to,
        subject=subject,
        html_content=html_content,
        text_content=text_content,
    )
