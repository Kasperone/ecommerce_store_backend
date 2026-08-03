"""Shared plumbing for **marketing** email: consent, unsubscribe links, headers.

Three things every marketing send needs and no transactional send should have:

1. an explicit opt-in check (``has_marketing_consent``),
2. a visible unsubscribe link in the body (``append_unsubscribe_footer``),
3. ``List-Unsubscribe`` + ``List-Unsubscribe-Post`` headers
   (``unsubscribe_headers``) so the mail client renders its own one-click
   control (RFC 8058).

Keeping all three here means a new marketing surface cannot accidentally ship
with only some of them — which is exactly how the campaign sender shipped with
none.

The suppression list (``crud.email_suppression``) is the other half and is
checked by the audience resolver; consent and suppression are complementary,
not redundant: consent is "did they ever say yes", suppression is "have they
since said stop", and the second wins.
"""

from __future__ import annotations

from urllib.parse import quote

from app.core.config import settings
from app.core.security import create_unsubscribe_token
from app.models.user_consent import MARKETING_CONSENT_TYPES

__all__ = [
    "MARKETING_CONSENT_TYPES",
    "append_unsubscribe_footer",
    "has_marketing_consent",
    "unsubscribe_headers",
    "unsubscribe_url",
]


def has_marketing_consent(consents: dict[str, bool]) -> bool:
    """True when the latest-per-type consent map shows an explicit opt-in.

    ``consents`` is the map returned by ``crud.user_consent.current_map``.
    Absent (never touched) and ``False`` (revoked) both mean **no** — under the
    Polish UŚUDE art. 10 / GDPR Art. 6(1)(a) the default is not consent.
    """
    return any(consents.get(kind) is True for kind in MARKETING_CONSENT_TYPES)


def unsubscribe_url(email: str) -> str:
    """The one-click unsubscribe URL for ``email``.

    Points at the **backend**, not the storefront: the link must keep working
    when the frontend is down or mid-deploy, and mailbox providers fetch it
    unattended. The route renders its own confirmation page.
    """
    token = create_unsubscribe_token(email)
    base = settings.BACKEND_URL.rstrip("/")
    return f"{base}/api/v1/newsletter/unsubscribe?token={quote(token)}"


def unsubscribe_headers(email: str) -> dict[str, str]:
    """``List-Unsubscribe`` + ``List-Unsubscribe-Post`` for a marketing send.

    The ``One-Click`` post directive is what makes Gmail/Yahoo render a native
    unsubscribe button and, per their 2024 bulk-sender rules, is required of any
    sender doing volume. Without it the only control the recipient has is the
    spam button, whose cost lands on the *transactional* mail sharing the domain.
    """
    url = unsubscribe_url(email)
    return {
        "List-Unsubscribe": f"<{url}>",
        "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
    }


_FOOTER_TEXTS: dict[str, dict[str, str]] = {
    "en": {
        "why": "You are receiving this because you opted in to marketing email.",
        "link": "Unsubscribe",
    },
    "pl": {
        "why": "Otrzymujesz tę wiadomość, ponieważ wyraziłeś zgodę na e-maile marketingowe.",
        "link": "Wypisz się",
    },
}


def append_unsubscribe_footer(html: str, *, email: str, lang: str = "en") -> str:
    """Append the visible unsubscribe footer to a marketing HTML body.

    Appended rather than templated around the body: campaign bodies are authored
    in the admin as free HTML, so wrapping them risks breaking the author's
    layout. A trailing block is layout-safe for any body.
    """
    texts = _FOOTER_TEXTS.get(lang, _FOOTER_TEXTS["en"])
    url = unsubscribe_url(email)
    return (
        f"{html}"
        f'<hr style="margin:32px 0 16px;border:none;border-top:1px solid #e5e7eb;" />'
        f'<p style="margin:0;font-size:12px;color:#6b7280;">'
        f"{texts['why']} "
        f'<a href="{url}" style="color:#6b7280;">{texts["link"]}</a>.'
        f"</p>"
    )
