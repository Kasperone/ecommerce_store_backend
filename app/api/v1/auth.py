import hmac
import logging
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Cookie, Depends, Header, HTTPException, Request, Response, status
from fastapi.security import OAuth2PasswordRequestForm
from jwt import PyJWTError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.dependencies import client_ip, get_current_active_user, get_db, user_agent
from app.core.email import (
    send_account_locked_email,
    send_already_registered_email,
    send_password_reset_email,
    send_verification_email,
    send_welcome_email,
)
from app.core.rate_limit import (
    get_email_or_ip_key,
    limiter,
    stash_json_email_for_rate_limit,
    stash_login_email_for_rate_limit,
)
from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    get_password_hash,
    verify_password,
)
from app.crud import company_user as crud_company_user
from app.crud import refresh_token as crud_refresh_token
from app.crud import user as crud_user
from app.crud.verification_token import verification_token
from app.models.company import Company
from app.models.user import User
from app.schemas.auth import (
    BusinessRegister,
    EmailResend,
    EmailVerification,
    GenericMessage,
    PasswordChange,
    PasswordReset,
    PasswordResetConfirm,
    SessionResponse,
    Token,
)
from app.schemas.user import UserCreate, UserResponse, UserUpdate

logger = logging.getLogger(__name__)


REFRESH_COOKIE_NAME = "refresh_token"
REFRESH_COOKIE_PATH = "/"


def _set_refresh_cookie(response: Response, token: str, expires_at: datetime) -> None:
    """Attach the rotating refresh token as an HttpOnly cookie.

    ``SameSite=Strict`` + ``HttpOnly`` are the real CSRF/XSS controls; path
    scoping was defense-in-depth. We use ``Path=/`` so the cookie rides on
    every same-origin request, which is what the Nuxt frontend's SSR plugin
    needs to validate the session before rendering protected routes (kills
    the flash-of-forbidden-content problem). ``Secure`` is dropped only in
    development so localhost http works.
    """
    max_age = int((expires_at - datetime.now(UTC)).total_seconds())
    if max_age < 0:
        max_age = 0
    response.set_cookie(
        key=REFRESH_COOKIE_NAME,
        value=token,
        max_age=max_age,
        httponly=True,
        secure=(settings.ENVIRONMENT != "development"),
        samesite="strict",
        path=REFRESH_COOKIE_PATH,
    )


def _clear_refresh_cookie(response: Response) -> None:
    response.delete_cookie(
        key=REFRESH_COOKIE_NAME,
        path=REFRESH_COOKIE_PATH,
    )


# Generic message returned by /register and /resend-verification regardless of
# whether the email exists, to prevent account enumeration via response shape.
GENERIC_REGISTRATION_MESSAGE = "If the email is available, a verification link has been sent."

# Generic message returned by /password-reset/request regardless of whether the
# email maps to an account.
GENERIC_PASSWORD_RESET_MESSAGE = (
    "If an account exists for this email, a password reset link has been sent."  # noqa: S105 -- user-facing message, not a credential
)

router = APIRouter()


@router.post(
    "/register",
    response_model=GenericMessage,
    status_code=status.HTTP_200_OK,
)
@limiter.limit("5/hour")
async def register(
    request: Request,
    user_in: UserCreate,
    db: Session = Depends(get_db),
):
    """
    Register a new user.

    Always returns a generic 200 response regardless of whether the email is
    already registered. This prevents account enumeration. If the email is
    already in use, an "account already exists" notice is sent out of band
    instead of a verification link.

    - **email**: Valid email address
    - **password**: Strong password (min 8 chars, 1 digit, 1 uppercase)
    - **first_name**: Optional first name
    - **last_name**: Optional last name
    - **phone**: Optional phone number
    """
    existing_user = crud_user.get_by_email(db, email=user_in.email)

    if existing_user:
        # Email already in use: notify the existing account holder out of band
        # and return the same generic body as a successful registration.
        try:
            send_already_registered_email(
                email_to=existing_user.email,
                username=existing_user.first_name or existing_user.email,
            )
        except Exception:
            logger.warning(
                "Email send failed: kind=%s user_id=%s",
                "already_registered",
                existing_user.id,
                exc_info=True,
            )
        return {"message": GENERIC_REGISTRATION_MESSAGE}

    user = crud_user.create(db, obj_in=user_in)

    try:
        token = verification_token.create_for_user(db, user_id=user.id)
        send_verification_email(
            email_to=user.email,
            username=user.first_name or user.email,
            token=token.token,
        )
    except Exception:
        # Don't fail the registration on email send failure; the user can
        # request a new verification email via /auth/resend-verification.
        logger.warning(
            "Email send failed: kind=%s user_id=%s",
            "verification",
            user.id,
            exc_info=True,
        )

    return {"message": GENERIC_REGISTRATION_MESSAGE}


@router.post(
    "/register-business",
    response_model=GenericMessage,
    status_code=status.HTTP_200_OK,
)
@limiter.limit("5/hour")
async def register_business(
    request: Request,
    payload: BusinessRegister,
    db: Session = Depends(get_db),
):
    """Self-register a B2B account (Wave D / D-D-5).

    Atomically creates the user (``UserRole.CUSTOMER``) + a ``pending`` company +
    an active ``COMPANY_ADMIN`` membership for that user, in one transaction. The
    company stays ``pending`` until an admin approves it via the existing admin
    Companies UI (open registration + manual approval). The user then goes
    through the normal email-verification path (reuses the ``register`` email
    flow), and the response is **anti-enumeration**: the same generic 200 body is
    returned whether the email is free or already taken (a duplicate sends the
    "already registered" notice out of band and creates nothing).

    - **email**, **password**, **first_name**, **last_name**, **phone**: the
      personal account (same rules as ``/register``).
    - **company_name** (required), **company_tax_id** (optional): the company.
    """
    existing_user = crud_user.get_by_email(db, email=payload.email)

    if existing_user:
        # Same anti-enumeration posture as ``/register``: notify the existing
        # holder out of band and return the generic body. No company is created.
        try:
            send_already_registered_email(
                email_to=existing_user.email,
                username=existing_user.first_name or existing_user.email,
            )
        except Exception:
            logger.warning(
                "Email send failed: kind=%s user_id=%s",
                "already_registered",
                existing_user.id,
                exc_info=True,
            )
        return {"message": GENERIC_REGISTRATION_MESSAGE}

    # Build user + company + membership in a single transaction. We construct the
    # User directly (rather than ``crud_user.create``, which commits on its own)
    # so the three rows commit together — a failure rolls back the whole signup.
    user = User(
        email=payload.email,
        hashed_password=get_password_hash(payload.password),
        first_name=payload.first_name,
        last_name=payload.last_name,
        phone=payload.phone or None,
        company_name=payload.company_name,
        company_tax_id=payload.company_tax_id or None,
    )
    db.add(user)
    db.flush()  # populate user.id without committing

    company = Company(
        name=payload.company_name,
        tax_id=payload.company_tax_id or None,
        status="pending",
        primary_user_id=user.id,
    )
    db.add(company)
    db.flush()  # populate company.id without committing

    # Active COMPANY_ADMIN membership (reuses CU-be's helper — flush, no commit).
    crud_company_user.create(db, company_id=company.id, user_id=user.id)

    db.commit()
    db.refresh(user)

    # Email verification is a post-create side effect (it commits its own token
    # row), exactly as in ``/register`` — keep it best-effort so an email outage
    # doesn't undo a successful signup.
    try:
        token = verification_token.create_for_user(db, user_id=user.id)
        send_verification_email(
            email_to=user.email,
            username=user.first_name or user.email,
            token=token.token,
        )
    except Exception:
        logger.warning(
            "Email send failed: kind=%s user_id=%s",
            "verification",
            user.id,
            exc_info=True,
        )

    return {"message": GENERIC_REGISTRATION_MESSAGE}


@router.post("/login", response_model=Token)
@limiter.limit("5/15minutes")
@limiter.limit("5/15minutes", key_func=get_email_or_ip_key)
async def login(
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    form_data: OAuth2PasswordRequestForm = Depends(),
    _stash: None = Depends(stash_login_email_for_rate_limit),
):
    """OAuth2 compatible token login.

    Returns the access JWT in the response body and sets a rotating refresh
    token as an HttpOnly cookie scoped to ``/api/v1/auth``.
    """
    # Look up user separately from password verification so we can attribute
    # failed attempts to the right account for lockout tracking.
    user = crud_user.get_by_email(db, email=form_data.username)

    invalid_credentials = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Incorrect email or password",
        headers={"WWW-Authenticate": "Bearer"},
    )

    # Locked accounts get the same generic 401 as a bad password — never
    # reveal the lockout state, that would let an attacker confirm the email
    # is valid and probe the lockout window.
    if user and crud_user.is_locked(user):
        raise invalid_credentials

    if not user or not verify_password(form_data.password, user.hashed_password):
        if user:
            just_locked = crud_user.register_failed_login(db, user=user)
            if just_locked:
                try:
                    send_account_locked_email(
                        email_to=user.email,
                        username=user.first_name or user.email,
                        lockout_minutes=int(crud_user.LOGIN_LOCKOUT_DURATION.total_seconds() // 60),
                    )
                except Exception:
                    logger.warning(
                        "Email send failed: kind=%s user_id=%s",
                        "account_locked",
                        user.id,
                        exc_info=True,
                    )
        raise invalid_credentials

    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Inactive user")

    if not user.is_verified:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Email not verified. Please check your email for verification link.",
        )

    crud_user.register_successful_login(db, user=user)

    access_token, _, _ = create_access_token(subject=user.id)
    refresh, refresh_jti, refresh_exp = create_refresh_token(subject=user.id)
    crud_refresh_token.create(
        db,
        user_id=user.id,
        jti=refresh_jti,
        token=refresh,
        expires_at=refresh_exp,
        ip=client_ip(request),
        user_agent=user_agent(request),
    )
    _set_refresh_cookie(response, refresh, refresh_exp)

    return {"access_token": access_token, "token_type": "bearer"}


@router.get("/me", response_model=UserResponse)
async def get_current_user_info(
    current_user: User = Depends(get_current_active_user),
):
    """
    Get current user information

    Requires authentication
    """
    return current_user


@router.put("/me", response_model=UserResponse)
async def update_current_user(
    user_in: UserUpdate,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """
    Update current user profile

    Requires authentication
    """
    user = crud_user.update(db, db_obj=current_user, obj_in=user_in)
    db.commit()
    db.refresh(user)
    return user


@router.post("/refresh", response_model=Token)
@limiter.limit("30/hour")
async def refresh_token(
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    refresh_cookie: str | None = Cookie(default=None, alias=REFRESH_COOKIE_NAME),
):
    """Rotate the refresh cookie and mint a new access token.

    Validates: signature/issuer/audience/expiry/type ⇒ DB row exists ⇒ token
    matches the stored hash ⇒ row not expired ⇒ row not already revoked.
    A revoked-but-validating token triggers reuse-detection: the entire
    refresh family for that user is revoked and a 401 is returned.
    """
    invalid = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate refresh token",
    )

    if not refresh_cookie:
        _clear_refresh_cookie(response)
        raise invalid

    try:
        decoded = decode_token(refresh_cookie, expected_type="refresh")
    except PyJWTError:
        _clear_refresh_cookie(response)
        raise invalid from None

    row = crud_refresh_token.get_by_jti(db, jti=decoded.jti)
    if row is None or not crud_refresh_token.matches(row, token=refresh_cookie):
        _clear_refresh_cookie(response)
        raise invalid

    if crud_refresh_token.is_revoked(row):
        # Reuse detection: the JWT validates and matches a row, but the row
        # was already rotated past. The legitimate client would be holding
        # the replacement; presenting an old token is a strong theft signal.
        crud_refresh_token.revoke_all_for_user(db, user_id=row.user_id)
        logger.warning(
            "refresh-token reuse detected; revoked family for user_id=%s jti=%s ip=%s",
            row.user_id,
            decoded.jti,
            client_ip(request),
        )
        _clear_refresh_cookie(response)
        raise invalid

    if crud_refresh_token.is_expired(row):
        crud_refresh_token.revoke(db, row=row)
        _clear_refresh_cookie(response)
        raise invalid

    user = crud_user.get(db, id=row.user_id)
    if user is None or not user.is_active:
        crud_refresh_token.revoke(db, row=row)
        _clear_refresh_cookie(response)
        raise invalid

    new_refresh, new_jti, new_exp = create_refresh_token(subject=user.id)
    crud_refresh_token.rotate(
        db,
        old=row,
        new_jti=new_jti,
        new_token=new_refresh,
        new_expires_at=new_exp,
        ip=client_ip(request),
        user_agent=user_agent(request),
    )
    _set_refresh_cookie(response, new_refresh, new_exp)

    access_token, _, _ = create_access_token(subject=user.id)
    return {"access_token": access_token, "token_type": "bearer"}


@router.post("/refresh/ssr", response_model=Token, include_in_schema=False)
async def refresh_token_ssr(
    db: Session = Depends(get_db),
    refresh_cookie: str | None = Cookie(default=None, alias=REFRESH_COOKIE_NAME),
    x_ssr_secret: str | None = Header(default=None, alias="X-SSR-Secret"),
):
    """Server-to-server session validation for SSR — mint an access token from a
    valid refresh cookie **without rotating** it.

    The Nuxt SSR server calls this on every server-rendered cold load so route
    middleware can resolve the visitor before paint (no flash-of-forbidden
    content). Unlike ``/refresh`` it is *read-only*: no rotation, no
    ``Set-Cookie``, no DB writes. That removes the rotation + Set-Cookie-replay
    race that surfaced as a hydration mismatch (and spurious ``/login``
    redirects) on hard reloads, and it sidesteps the user-facing ``30/hour``
    refresh limit a per-cold-load SSR refresh would otherwise burn — which is
    why this route is deliberately *not* IP-rate-limited (a single SSR origin
    fans out every visitor's refresh through one IP).

    Gated by ``SSR_SHARED_SECRET`` (sent as ``X-SSR-Secret``) so an ordinary
    client — even one holding a stolen cookie — cannot reach this non-rotating
    path and mint access tokens indefinitely. An empty secret disables the
    endpoint (403), failing closed.

    A revoked/expired cookie returns 401 but, unlike ``/refresh``, does **not**
    trigger family-wide reuse-detection revocation: at SSR time a stale cookie
    is far likelier a benign rotation race (the client just rotated in another
    tab) than theft, and nuking the family would log the user out everywhere.
    """
    expected = settings.SSR_SHARED_SECRET
    if not expected or not x_ssr_secret or not hmac.compare_digest(x_ssr_secret, expected):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")

    invalid = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate refresh token",
    )
    if not refresh_cookie:
        raise invalid
    try:
        decoded = decode_token(refresh_cookie, expected_type="refresh")
    except PyJWTError:
        raise invalid from None

    row = crud_refresh_token.get_by_jti(db, jti=decoded.jti)
    if row is None or not crud_refresh_token.matches(row, token=refresh_cookie):
        raise invalid
    # Read-only path: treat a revoked/expired cookie as "logged out for this
    # render" without touching the refresh family (see docstring).
    if crud_refresh_token.is_revoked(row) or crud_refresh_token.is_expired(row):
        raise invalid

    user = crud_user.get(db, id=row.user_id)
    if user is None or not user.is_active:
        raise invalid

    access_token, _, _ = create_access_token(subject=user.id)
    return {"access_token": access_token, "token_type": "bearer"}


@router.post(
    "/logout",
    response_model=GenericMessage,
    status_code=status.HTTP_200_OK,
)
async def logout(
    response: Response,
    db: Session = Depends(get_db),
    refresh_cookie: str | None = Cookie(default=None, alias=REFRESH_COOKIE_NAME),
):
    """Revoke the current refresh row (if any) and clear the cookie.

    Idempotent: invalid or missing cookies still return 200, so a stale
    client UI can call /logout without a special-case error path.
    """
    if refresh_cookie:
        try:
            decoded = decode_token(refresh_cookie, expected_type="refresh")
        except PyJWTError:
            decoded = None
        if decoded is not None:
            row = crud_refresh_token.get_by_jti(db, jti=decoded.jti)
            if row is not None and not crud_refresh_token.is_revoked(row):
                crud_refresh_token.revoke(db, row=row)
    _clear_refresh_cookie(response)
    return {"message": "Logged out."}


@router.post(
    "/logout-all",
    response_model=GenericMessage,
    status_code=status.HTTP_200_OK,
)
async def logout_all(
    response: Response,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Revoke every refresh token for the current user and bump
    ``password_changed_at`` so existing access tokens stop validating.
    Useful for "sign out everywhere" after a credential scare."""
    crud_refresh_token.revoke_all_for_user(db, user_id=current_user.id)
    # Push iat-floor forward so any access token issued before now is
    # rejected by get_current_user. JWT iat is whole seconds, so add one
    # second to also invalidate tokens minted in this same whole second.
    current_user.password_changed_at = datetime.now(UTC).replace(tzinfo=None) + timedelta(seconds=1)
    db.add(current_user)
    db.commit()
    _clear_refresh_cookie(response)
    return {"message": "Signed out of all sessions."}


@router.get("/sessions", response_model=list[SessionResponse])
async def list_sessions(
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
    refresh_cookie: str | None = Cookie(default=None, alias=REFRESH_COOKIE_NAME),
):
    """List the caller's active login sessions (live ``refresh_tokens`` rows).

    Only the authenticated user's sessions are returned, newest-first, and
    only those that are neither revoked nor expired. The session whose ``jti``
    matches the caller's refresh cookie is flagged ``current=True``; a
    missing or invalid cookie means no row is current.
    """
    current_jti: str | None = None
    if refresh_cookie:
        try:
            current_jti = decode_token(refresh_cookie, expected_type="refresh").jti
        except PyJWTError:
            current_jti = None

    rows = crud_refresh_token.list_active_for_user(db, user_id=current_user.id)
    return [
        SessionResponse(
            id=row.id,
            created_at=row.created_at,
            created_ip=row.created_ip,
            user_agent=row.user_agent,
            current=(current_jti is not None and row.jti == current_jti),
            expires_at=row.expires_at,
        )
        for row in rows
    ]


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_session(
    session_id: int,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """Revoke one of the caller's active sessions by id.

    Owner-gated: a row that belongs to another user, an unknown id, or a row
    that is already revoked/expired (i.e. not an active session) all return
    404 — never 403 — so the endpoint can't be used to enumerate session ids.

    Revoking the *current* session is allowed and not special-cased: the next
    /refresh will fail and the client logs out. This does NOT clear the
    refresh cookie (that is the client's /logout responsibility).
    """
    row = crud_refresh_token.get(db, id=session_id)
    not_found = HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")

    if (
        row is None
        or row.user_id != current_user.id
        or crud_refresh_token.is_revoked(row)
        or crud_refresh_token.is_expired(row)
    ):
        raise not_found

    crud_refresh_token.revoke(db, row=row)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/verify-email", status_code=status.HTTP_200_OK)
async def verify_email(
    verification: EmailVerification,
    db: Session = Depends(get_db),
):
    """
    Verify user email with token

    - **token**: Verification token from email

    Returns success message
    """
    # Validate token
    is_valid, error_msg = verification_token.is_valid(db, token=verification.token)

    if not is_valid:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=error_msg)

    # Get token and user
    token_obj = verification_token.get_by_token(db, token=verification.token)
    if not token_obj:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid verification token"
        )

    user = crud_user.get(db, id=token_obj.user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    # Check if already verified
    if user.is_verified:
        return {"message": "Email already verified"}

    # Mark user as verified
    user.is_verified = True
    db.commit()

    # Delete verification token
    verification_token.delete_by_token(db, token=verification.token)

    # Send welcome email
    try:
        send_welcome_email(email_to=user.email, username=user.first_name or user.email)
    except Exception:
        logger.warning(
            "Email send failed: kind=%s user_id=%s",
            "welcome",
            user.id,
            exc_info=True,
        )

    return {"message": "Email verified successfully"}


@router.post(
    "/password-reset/request",
    response_model=GenericMessage,
    status_code=status.HTTP_200_OK,
)
@limiter.limit("3/hour", key_func=get_email_or_ip_key)
async def request_password_reset(
    request: Request,
    payload: PasswordReset,
    db: Session = Depends(get_db),
    _stash: None = Depends(stash_json_email_for_rate_limit),
):
    """Begin a password reset.

    Always returns a generic 200 response so the endpoint cannot be used to
    enumerate accounts. If the email matches an active user, a one-time
    reset link (valid for 1 hour) is emailed out of band.
    """
    generic = GENERIC_PASSWORD_RESET_MESSAGE
    user = crud_user.get_by_email(db, email=payload.email)

    if user and user.is_active:
        token = crud_user.issue_password_reset_token(db, user=user)
        try:
            send_password_reset_email(
                email_to=user.email,
                username=user.first_name or user.email,
                token=token,
            )
        except Exception:
            logger.warning(
                "Email send failed: kind=%s user_id=%s",
                "password_reset",
                user.id,
                exc_info=True,
            )

    return {"message": generic}


@router.post(
    "/password-reset/confirm",
    response_model=GenericMessage,
    status_code=status.HTTP_200_OK,
)
async def confirm_password_reset(
    payload: PasswordResetConfirm,
    response: Response,
    db: Session = Depends(get_db),
):
    """Complete a password reset by submitting the emailed token plus a new
    password. Generic 400 on any token/expiry failure to avoid token
    enumeration.

    Successful reset revokes the user's entire refresh-token family in
    ``consume_password_reset_token``. The refresh cookie on *this* device
    points at one of those now-revoked rows, so clear it here too — the
    next /refresh would 401 either way, but a clean cookie avoids a
    confusing one-shot 401 on the calling browser.
    """
    ok, _ = crud_user.consume_password_reset_token(
        db, token=payload.token, new_password=payload.new_password
    )
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired password reset token.",
        )
    _clear_refresh_cookie(response)
    return {"message": "Password has been reset. You can now sign in."}


@router.post(
    "/change-password",
    response_model=GenericMessage,
    status_code=status.HTTP_200_OK,
)
async def change_password(
    payload: PasswordChange,
    response: Response,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """Change the password of the authenticated user. Verifies the current
    password before applying the change.

    The change revokes every refresh-token row for the user. The frontend's
    next /refresh proactively scheduled by ``useApi`` would 401 anyway —
    clearing the cookie here makes the post-change UX a clean re-login.
    """
    if payload.current_password == payload.new_password:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="New password must differ from the current password.",
        )
    ok = crud_user.change_password(
        db,
        user=current_user,
        current_password=payload.current_password,
        new_password=payload.new_password,
    )
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Current password is incorrect.",
        )
    _clear_refresh_cookie(response)
    return {"message": "Password updated."}


@router.post(
    "/resend-verification",
    response_model=GenericMessage,
    status_code=status.HTTP_200_OK,
)
@limiter.limit("3/hour", key_func=get_email_or_ip_key)
async def resend_verification_email(
    request: Request,
    resend: EmailResend,
    db: Session = Depends(get_db),
    _stash: None = Depends(stash_json_email_for_rate_limit),
):
    """
    Resend verification email.

    Always returns a generic 200 response regardless of whether the email is
    registered or already verified. Mirrors the response shape of /register
    so the two endpoints cannot be used to enumerate accounts.

    - **email**: User email address
    """
    user = crud_user.get_by_email(db, email=resend.email)

    if user and not user.is_verified:
        token = verification_token.replace_for_user(db, user_id=user.id)
        try:
            send_verification_email(
                email_to=user.email,
                username=user.first_name or user.email,
                token=token.token,
            )
        except Exception:
            logger.warning(
                "Email send failed: kind=%s user_id=%s",
                "verification_resend",
                user.id,
                exc_info=True,
            )

    return {"message": GENERIC_REGISTRATION_MESSAGE}
