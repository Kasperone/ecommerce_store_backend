from collections.abc import Generator
from datetime import UTC

from fastapi import Depends, Header, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer
from jwt import PyJWTError
from sqlalchemy.orm import Session

from app.core.database import SessionLocal
from app.core.security import decode_token
from app.crud import company_user as crud_company_user
from app.crud import user as crud_user
from app.crud.company_user import CompanyMembership
from app.models.company_user import CompanyRole
from app.models.user import User, UserRole

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login")

# Width of the user_agent column on refresh_tokens and admin_audit_events.
USER_AGENT_MAX_LEN = 500


def get_db() -> Generator:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def client_ip(request: Request) -> str | None:
    """Return the request's client IP, or None when the transport doesn't expose one."""
    if request.client is None:
        return None
    return request.client.host


def user_agent(request: Request) -> str | None:
    """Return the User-Agent header, truncated to the storage column width."""
    ua = request.headers.get("user-agent")
    if not ua:
        return None
    return ua[:USER_AGENT_MAX_LEN]


async def get_current_user(
    db: Session = Depends(get_db),
    token: str = Depends(oauth2_scheme),
) -> User:
    """Resolve the bearer access token to a user.

    Rejects tokens whose ``iat`` predates ``user.password_changed_at`` —
    that's how /logout-all and password rotations invalidate access tokens
    without a denylist.
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )

    try:
        decoded = decode_token(token, expected_type="access")
    except PyJWTError:
        raise credentials_exception from None

    try:
        user_id = int(decoded.subject)
    except ValueError:
        raise credentials_exception from None

    user = crud_user.get(db, id=user_id)
    if user is None:
        raise credentials_exception

    # password_changed_at is stored naive UTC; align the comparison.
    pwd_changed = user.password_changed_at
    if pwd_changed is not None:
        iat = decoded.issued_at
        if iat.tzinfo is not None:
            iat = iat.astimezone(UTC).replace(tzinfo=None)
        # JWT iat is int seconds; floor pwd_changed (microseconds) to match.
        if iat < pwd_changed.replace(microsecond=0):
            raise credentials_exception

    return user


async def get_optional_user(
    db: Session = Depends(get_db),
    authorization: str | None = Header(default=None),
) -> User | None:
    """Resolve the bearer access token to a user when present and valid, else None.

    Never raises — endpoints that serve both guests and authed callers (guest
    checkout, coupon preview, cookie consent) use this instead of
    ``get_current_user``. Anything wrong — no header, malformed scheme,
    bad/expired token, unknown user, inactive account, token issued before the
    last password change — resolves to ``None`` so the caller is treated as a
    guest. Applies the same ``iat < password_changed_at`` floor as
    ``get_current_user`` so a "signed out everywhere" token can't keep acting
    as the user on optional-auth routes.
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

    user = crud_user.get(db, id=user_id)
    if user is None or not user.is_active:
        return None

    pwd_changed = user.password_changed_at
    if pwd_changed is not None:
        iat = decoded.issued_at
        if iat.tzinfo is not None:
            iat = iat.astimezone(UTC).replace(tzinfo=None)
        if iat < pwd_changed.replace(microsecond=0):
            return None

    return user


async def get_current_active_user(
    current_user: User = Depends(get_current_user),
) -> User:
    if not current_user.is_active:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Inactive user",
        )
    return current_user


async def get_current_admin(
    current_user: User = Depends(get_current_active_user),
) -> User:
    if current_user.role != UserRole.ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not enough permissions",
        )
    return current_user


def require_capability(cap: str):
    """Build a dependency that requires staff to hold capability ``cap``.

    Layered on ``get_current_admin`` (so a non-staff caller still 403s at the
    role check, and FastAPI dedupes the shared ``get_current_admin`` when a route
    also declares ``get_audit_writer``). An **owner** implicitly holds every
    capability; a scoped staffer must have ``cap`` in ``users.capabilities``.
    Gate every write/DELETE admin route with this; GET routes stay open to any
    staff. See ``app/core/capabilities.py`` for the taxonomy.
    """

    async def _dep(current_user: User = Depends(get_current_admin)) -> User:
        if current_user.is_owner or cap in (current_user.capabilities or []):
            return current_user
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not enough permissions",
        )

    return _dep


async def get_current_company_member(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> CompanyMembership:
    """Resolve the caller's active company membership (company + B2B role).

    Mirrors ``get_current_admin`` for the B2B portal: the caller must have an
    ``active`` ``company_users`` membership (or be a company's denormalized
    primary contact, which resolves as ``COMPANY_ADMIN`` — see
    ``crud_company_user.get_active_membership``). A caller who is no company's
    member gets a **404** ("you are not a B2B account") rather than a 403 — the
    B2B surfaces don't exist for them, and we don't leak that the route is there.
    Returns a ``CompanyMembership`` (``company`` + ``role`` + the concrete row or
    a synthesized context) for the route to use.
    """
    membership = crud_company_user.get_active_membership(db, user_id=current_user.id)
    if membership is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No company for this account",
        )
    return membership


async def require_company_admin(
    membership: CompanyMembership = Depends(get_current_company_member),
) -> CompanyMembership:
    """Require the caller to be a ``COMPANY_ADMIN`` of their company.

    Layered on ``get_current_company_member`` (so a non-member still 404s). A
    member whose role is not ``COMPANY_ADMIN`` (BUYER/APPROVER/VIEWER) gets a
    **403** — they are a B2B account but lack the privilege. Used by the
    company-profile + team-management routes (CP-be / TM-be)."""
    if membership.role != CompanyRole.COMPANY_ADMIN.value:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not enough permissions",
        )
    return membership
