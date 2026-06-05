import logging

from fastapi import APIRouter, Depends, Request, status

from app.core.config import settings
from app.core.email import send_contact_email
from app.core.rate_limit import (
    get_email_or_ip_key,
    limiter,
    stash_json_email_for_rate_limit,
)
from app.schemas.contact import ContactRequest, ContactResponse

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post(
    "/contact",
    response_model=ContactResponse,
    status_code=status.HTTP_200_OK,
)
@limiter.limit("5/hour", key_func=get_email_or_ip_key)
async def submit_contact_form(
    request: Request,
    payload: ContactRequest,
    _stash: None = Depends(stash_json_email_for_rate_limit),
) -> ContactResponse:
    """Public contact form — emails the store inbox via Resend.

    Rate-limited 5/hour per submitter email (falling back to IP). The send is
    best-effort: a provider failure is logged and swallowed so the submitter
    still gets a success response (mirrors the other transactional sends).
    """
    # Reuse the configured store inbox; fall back to the from-address so the
    # email always has a valid recipient even before STORE_EMAIL is set.
    recipient = settings.STORE_EMAIL or settings.EMAIL_FROM

    try:
        send_contact_email(
            email_to=recipient,
            name=payload.name,
            reply_to=payload.email,
            subject=payload.subject,
            message=payload.message,
        )
    except Exception:
        logger.warning(
            "Email send failed: kind=%s recipient=%s",
            "contact",
            recipient,
            exc_info=True,
        )

    return ContactResponse(status="sent")
