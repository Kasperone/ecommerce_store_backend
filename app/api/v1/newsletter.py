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

from fastapi import APIRouter, Depends, Request, status
from sqlalchemy.orm import Session

from app.core.dependencies import client_ip, get_db, user_agent
from app.core.rate_limit import (
    get_email_or_ip_key,
    limiter,
    stash_json_email_for_rate_limit,
)
from app.crud import newsletter as crud_newsletter
from app.crud import user as crud_user
from app.crud import user_consent as crud_user_consent
from app.schemas.newsletter import NewsletterSubscribeRequest, NewsletterSubscribeResponse

router = APIRouter()

NEWSLETTER_SOURCE = "footer"
NEWSLETTER_CONSENT_SOURCE = "newsletter_signup"


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
