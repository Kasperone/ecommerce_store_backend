"""Public newsletter subscribe API.

Unauthenticated, like the public banners/catalog endpoints. ``POST /subscribe``
is **idempotent** and **anti-enumeration**: it always returns a generic 200
``{"status": "subscribed"}`` whether the email is new, already subscribed, or
reactivated — it never reveals prior state (mirrors
``/auth/password-reset/request``). Rate-limited per email-or-IP (the
``stash_json_email_for_rate_limit`` dep makes the limiter key off the email).

On success it idempotently upserts a ``newsletter_subscribers`` row, and — only
when the email maps to an existing user — ALSO appends a marketing consent to
the ``user_consents`` audit log. For an anonymous email the subscriber row is
itself the consent record (it captures ``source`` / ``ip`` / ``user_agent``).
"""

import jwt
from fastapi import APIRouter, Depends, Query, Request, status
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.core.dependencies import client_ip, get_db, user_agent
from app.core.rate_limit import (
    get_email_or_ip_key,
    limiter,
    stash_json_email_for_rate_limit,
)
from app.core.security import decode_token
from app.crud import email_suppression as crud_email_suppression
from app.crud import newsletter as crud_newsletter
from app.crud import user as crud_user
from app.crud import user_consent as crud_user_consent
from app.models.user_consent import MARKETING_CONSENT_TYPES
from app.schemas.newsletter import (
    NewsletterSubscribeRequest,
    NewsletterSubscribeResponse,
    NewsletterUnsubscribeResponse,
)

router = APIRouter()

NEWSLETTER_SOURCE = "footer"
NEWSLETTER_CONSENT_SOURCE = "newsletter_signup"
UNSUBSCRIBE_CONSENT_SOURCE = "unsubscribe_link"


@router.post(
    "/subscribe",
    response_model=NewsletterSubscribeResponse,
    status_code=status.HTTP_200_OK,
)
@limiter.limit("10/hour", key_func=get_email_or_ip_key)
async def subscribe(
    request: Request,
    payload: NewsletterSubscribeRequest,
    db: Session = Depends(get_db),
    _stash: None = Depends(stash_json_email_for_rate_limit),
):
    """Opt an email in to the marketing newsletter (public, idempotent).

    Always returns a generic 200 so the endpoint cannot be used to enumerate
    subscribers. Re-subscribing reactivates the existing row rather than
    creating a duplicate. When the email belongs to a registered user a
    ``user_consents`` newsletter row is appended for the GDPR audit trail.
    """
    ip = client_ip(request)
    ua = user_agent(request)

    crud_newsletter.subscribe(
        db,
        email=payload.email,
        source=NEWSLETTER_SOURCE,
        ip=ip,
        user_agent=ua,
    )

    user = crud_user.get_by_email(db, email=payload.email)
    if user is not None:
        crud_user_consent.record(
            db,
            user_id=user.id,
            consent_type="newsletter",
            granted=True,
            source=NEWSLETTER_CONSENT_SOURCE,
            ip=ip,
            user_agent=ua,
        )

    db.commit()
    return {"status": "subscribed"}


def _apply_unsubscribe(db: Session, *, email: str, ip: str | None, ua: str | None) -> None:
    """Opt ``email`` out of every marketing channel, in one transaction.

    Three writes because marketing recipients come from three shapes and the
    same human can be in all of them at once:

    1. the **suppression row** — the single check every marketing sender makes,
       and the only one that covers an address with no account;
    2. the **newsletter subscriber row** (if any) parked via ``is_active=False``,
       so a later re-subscribe reactivates it rather than duplicating;
    3. a **withdrawal row per marketing consent type** (if the address maps to a
       user), because ``user_consents`` is the GDPR audit trail and it would
       otherwise still read "granted" after the person opted out.

    Idempotent: re-running suppresses nothing new and appends only fresh
    withdrawal rows, which is correct for an append-only log.
    """
    crud_email_suppression.suppress(db, email=email, reason="unsubscribe_link")
    crud_newsletter.unsubscribe(db, email=email)

    user = crud_user.get_by_email(db, email=email)
    if user is not None:
        for consent_type in MARKETING_CONSENT_TYPES:
            crud_user_consent.record(
                db,
                user_id=user.id,
                consent_type=consent_type,
                granted=False,
                source=UNSUBSCRIBE_CONSENT_SOURCE,
                ip=ip,
                user_agent=ua,
            )
    db.commit()


def _email_from_token(token: str) -> str | None:
    """The address a valid unsubscribe token is for, else ``None``."""
    try:
        decoded = decode_token(token, expected_type="unsubscribe")
    except jwt.PyJWTError:
        return None
    return decoded.subject or None


_PAGE_CSS = (
    "font-family:system-ui,-apple-system,'Segoe UI',sans-serif;"
    "max-width:32rem;margin:4rem auto;padding:0 1.5rem;color:#111827;line-height:1.6;"
)


def _page(title: str, body: str, *, status_code: int) -> HTMLResponse:
    return HTMLResponse(
        f"<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<meta name='robots' content='noindex'>"
        f"<title>{title}</title></head>"
        f'<body style="{_PAGE_CSS}">'
        f"<h1 style='font-size:1.5rem;'>{title}</h1>{body}"
        f"</body></html>",
        status_code=status_code,
    )


@router.get("/unsubscribe", response_class=HTMLResponse, include_in_schema=False)
async def unsubscribe_page(
    token: str = Query(..., description="Signed unsubscribe token from the email footer"),
    db: Session = Depends(get_db),
):
    """Human-facing unsubscribe link — opts out, then confirms in the browser.

    Serves its **own** HTML rather than redirecting to the storefront on
    purpose: this link is clicked from mail clients long after it was sent, and
    it must keep working while the frontend is down, mid-deploy, or has had its
    routes reshuffled. A dead unsubscribe link is not a neutral failure — the
    recipient's next move is the spam button, which costs the sending domain far
    more than this page costs to maintain.

    Excluded from the OpenAPI schema: it is a link target for humans and mailbox
    providers, not part of the JSON API surface the frontend generates types from.
    """
    email = _email_from_token(token)
    if email is None:
        return _page(
            "Link expired or invalid",
            "<p>We could not read this unsubscribe link. Please use the link in a more "
            "recent email, or change your preferences from your account settings.</p>",
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    _apply_unsubscribe(db, email=email, ip=None, ua=None)
    return _page(
        "You have been unsubscribed",
        "<p>You will no longer receive marketing email from us.</p>"
        "<p style='color:#6b7280;font-size:0.875rem;'>Order confirmations, delivery updates "
        "and other messages about purchases you make will still be sent — those are not "
        "marketing.</p>",
        status_code=status.HTTP_200_OK,
    )


@router.post(
    "/unsubscribe",
    response_model=NewsletterUnsubscribeResponse,
    status_code=status.HTTP_200_OK,
)
async def unsubscribe_one_click(
    token: str = Query(..., description="Signed unsubscribe token from the email footer"),
    db: Session = Depends(get_db),
):
    """RFC 8058 One-Click target for the ``List-Unsubscribe-Post`` header.

    Mailbox providers POST here with no user interaction when the recipient uses
    the client's native unsubscribe button, so it takes no body and no auth —
    the token in the query string is the whole credential. Deliberately **not**
    rate-limited: throttling a provider's one-click call would strand the opt-out
    and is precisely what the bulk-sender rules forbid.

    Returns 200 even for a token we cannot read: the provider surfaces a failure
    to the recipient as "unsubscribe didn't work", pushing them to the spam
    button, and a bad token means nobody is subscribed under it anyway.
    """
    email = _email_from_token(token)
    if email is not None:
        _apply_unsubscribe(db, email=email, ip=None, ua=None)
    return {"status": "unsubscribed"}
