"""Public cookie-consent API — GDPR/ePrivacy proof-of-choice storage.

Backs the FE cookie banner + the footer "Cookie settings" reopen control. Both
endpoints are PUBLIC and never 401 a guest:

- ``POST /cookie-consent`` records a choice (append-only). If a *valid* bearer is
  present the row is attached to the user; otherwise it's a guest row keyed by
  ``anon_id`` (the FE client-id cookie). Captures ip/ua + the client's Global
  Privacy Control flag. ``necessary`` is always coerced true server-side. Modestly
  rate-limited.
- ``GET /cookie-consent?anon_id=`` returns the latest choice (200 with null
  fields if none) — also resolving the authed user when a bearer is present.

Optional auth: the shared ``get_optional_user`` dependency resolves the current
user IF a valid bearer is present but never raises — guests must be able to call
both endpoints.
"""

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from app.core.dependencies import client_ip, get_db, get_optional_user, user_agent
from app.core.rate_limit import limiter
from app.crud import cookie_consent as crud_cookie_consent
from app.models.user import User
from app.schemas.cookie_consent import (
    CookieConsentCategories,
    CookieConsentRequest,
    CookieConsentResponse,
)

router = APIRouter()


@router.post("/cookie-consent", response_model=CookieConsentResponse)
@limiter.limit("30/minute")
async def record_cookie_consent(
    request: Request,
    payload: CookieConsentRequest,
    db: Session = Depends(get_db),
    current_user: User | None = Depends(get_optional_user),
    ip: str | None = Depends(client_ip),
    ua: str | None = Depends(user_agent),
) -> CookieConsentResponse:
    """Record a cookie-consent choice (guest or authed). Append-only."""
    obj = crud_cookie_consent.record(
        db,
        anon_id=payload.anon_id,
        user_id=current_user.id if current_user else None,
        categories=payload.categories.model_dump(),
        gpc=payload.gpc,
        policy_version=payload.policy_version,
        ip=ip,
        user_agent=ua,
    )
    db.commit()
    db.refresh(obj)
    return CookieConsentResponse(
        categories=CookieConsentCategories(**obj.categories),
        gpc=obj.gpc,
        policy_version=obj.policy_version,
        created_at=obj.created_at,
    )


@router.get("/cookie-consent", response_model=CookieConsentResponse | None)
async def get_cookie_consent(
    anon_id: str | None = Query(default=None, max_length=64),
    db: Session = Depends(get_db),
    current_user: User | None = Depends(get_optional_user),
) -> CookieConsentResponse | None:
    """Return the latest cookie-consent choice (200 null if none)."""
    obj = crud_cookie_consent.current_for(
        db,
        anon_id=anon_id,
        user_id=current_user.id if current_user else None,
    )
    if obj is None:
        return None
    return CookieConsentResponse(
        categories=CookieConsentCategories(**obj.categories),
        gpc=obj.gpc,
        policy_version=obj.policy_version,
        created_at=obj.created_at,
    )
