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

The optional-auth helper below resolves the current user IF a valid bearer is
present but never raises — guests must be able to call both endpoints. It does
NOT touch the shared ``get_current_user`` dependency.
"""

from fastapi import APIRouter, Depends, Header, Query, Request
from jwt import PyJWTError
from sqlalchemy.orm import Session

from app.core.dependencies import client_ip, get_db, user_agent
from app.core.rate_limit import limiter
from app.core.security import decode_token
from app.crud import cookie_consent as crud_cookie_consent
from app.crud import user as crud_user
from app.models.user import User
from app.schemas.cookie_consent import (
    CookieConsentCategories,
    CookieConsentRequest,
    CookieConsentResponse,
)

router = APIRouter()


def get_current_user_optional(
    db: Session = Depends(get_db),
    authorization: str | None = Header(default=None),
) -> User | None:
    """Resolve the bearer access token to a user, or ``None`` — never raises.

    Public endpoints that *optionally* personalize for an authed caller use this
    instead of ``get_current_user`` (which 401s on a missing/invalid token).
    Anything wrong — no header, malformed scheme, bad/expired token, unknown
    user — resolves to ``None`` so a guest is served normally.
    """
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    try:
        decoded = decode_token(token, expected_type="access")
        user_id = int(decoded.subject)
    except (PyJWTError, ValueError):
        return None
    return crud_user.get(db, id=user_id)


@router.post("/cookie-consent", response_model=CookieConsentResponse)
@limiter.limit("30/minute")
async def record_cookie_consent(
    request: Request,
    payload: CookieConsentRequest,
    db: Session = Depends(get_db),
    current_user: User | None = Depends(get_current_user_optional),
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
    current_user: User | None = Depends(get_current_user_optional),
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
