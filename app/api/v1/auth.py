import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response, status
from fastapi.security import OAuth2PasswordRequestForm
from jose import JWTError
from sqlalchemy.orm import Session

from app.core.dependencies import get_db, get_current_active_user
from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    verify_password,
)
from app.core.config import settings
from app.core.email import (
    send_verification_email,
    send_welcome_email,
    send_already_registered_email,
    send_password_reset_email,
    send_account_locked_email,
)
from app.core.rate_limit import (
    get_email_or_ip_key,
    limiter,
    stash_json_email_for_rate_limit,
    stash_login_email_for_rate_limit,
)
from app.crud import user as crud_user
from app.crud import refresh_token as crud_refresh_token
from app.crud.verification_token import verification_token
from app.schemas.auth import (
    Token,
    EmailVerification,
    EmailResend,
    GenericMessage,
    PasswordChange,
    PasswordReset,
    PasswordResetConfirm,
)
from app.schemas.user import UserCreate, UserResponse, UserUpdate
from app.models.user import User


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
    max_age = int((expires_at - datetime.now(timezone.utc)).total_seconds())
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


def _client_ip(request: Request) -> Optional[str]:
    if request.client is None:
        return None
    return request.client.host


def _user_agent(request: Request) -> Optional[str]:
    ua = request.headers.get("user-agent")
    if not ua:
        return None
    return ua[:512]


# Generic message returned by /register and /resend-verification regardless of
# whether the email exists, to prevent account enumeration via response shape.
GENERIC_REGISTRATION_MESSAGE = (
    "If the email is available, a verification link has been sent."
)

# Generic message returned by /password-reset/request regardless of whether the
# email maps to an account.
GENERIC_PASSWORD_RESET_MESSAGE = (
    "If an account exists for this email, a password reset link has been sent."
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
                        lockout_minutes=int(
                            crud_user.LOGIN_LOCKOUT_DURATION.total_seconds() // 60
                        ),
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
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Inactive user"
        )

    if not user.is_verified:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Email not verified. Please check your email for verification link."
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
        ip=_client_ip(request),
        user_agent=_user_agent(request),
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
    return user


@router.post("/refresh", response_model=Token)
@limiter.limit("30/hour")
async def refresh_token(
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    refresh_cookie: Optional[str] = Cookie(default=None, alias=REFRESH_COOKIE_NAME),
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
    except JWTError:
        _clear_refresh_cookie(response)
        raise invalid

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
            _client_ip(request),
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
        ip=_client_ip(request),
        user_agent=_user_agent(request),
    )
    _set_refresh_cookie(response, new_refresh, new_exp)

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
    refresh_cookie: Optional[str] = Cookie(default=None, alias=REFRESH_COOKIE_NAME),
):
    """Revoke the current refresh row (if any) and clear the cookie.

    Idempotent: invalid or missing cookies still return 200, so a stale
    client UI can call /logout without a special-case error path.
    """
    if refresh_cookie:
        try:
            decoded = decode_token(refresh_cookie, expected_type="refresh")
        except JWTError:
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
    current_user.password_changed_at = (
        datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(seconds=1)
    )
    db.add(current_user)
    db.commit()
    _clear_refresh_cookie(response)
    return {"message": "Signed out of all sessions."}


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
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=error_msg
        )
    
    # Get token and user
    token_obj = verification_token.get_by_token(db, token=verification.token)
    if not token_obj:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid verification token"
        )
    
    user = crud_user.get(db, id=token_obj.user_id)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )
    
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
        send_welcome_email(
            email_to=user.email,
            username=user.first_name or user.email
        )
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
