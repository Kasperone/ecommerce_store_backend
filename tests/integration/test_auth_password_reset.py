"""Integration tests for the unauthenticated password-reset flow:

- ``POST /api/v1/auth/password-reset/request``  — issues an opaque token
  and sends the email out of band; always returns generic 200.
- ``POST /api/v1/auth/password-reset/confirm`` — consumes the token,
  rotates the password, clears the reset state, bumps
  ``password_changed_at``, and (per the recheck Phase 1.1 contract) must
  revoke every active refresh token for that user.

The refresh-revocation test is ``xfail(strict=True)`` until recheck
Phase 1.1 ships: this captures the contract today and will turn into an
XPASS (= failure) the moment the fix lands, signalling that the marker
should be removed.
"""

from datetime import timedelta

import pytest

from app.api.v1.auth import GENERIC_PASSWORD_RESET_MESSAGE
from app.core.security import decode_token, verify_password
from app.crud import refresh_token as crud_refresh_token
from app.crud.user import _utcnow_naive
from tests.factories.user import DEFAULT_PASSWORD


pytestmark = pytest.mark.integration


REQUEST_PATH = "/api/v1/auth/password-reset/request"
CONFIRM_PATH = "/api/v1/auth/password-reset/confirm"
LOGIN_PATH = "/api/v1/auth/login"


# ---------------------------------------------------------------------------
# /password-reset/request
# ---------------------------------------------------------------------------


async def test_request_returns_generic_200_for_known_email_and_sends_email(
    client, db_session, captured_emails, reset_limiter, user_factory
):
    user = user_factory(email="reset@example.com")

    response = await client.post(REQUEST_PATH, json={"email": "reset@example.com"})
    assert response.status_code == 200
    assert response.json() == {"message": GENERIC_PASSWORD_RESET_MESSAGE}

    sent = [e for e in captured_emails if e["email_to"] == "reset@example.com"]
    assert [e["kind"] for e in sent] == ["password_reset"]
    # Token must travel to the email helper so the user can click the link.
    assert sent[0].get("token")

    db_session.refresh(user)
    assert user.password_reset_token_hash is not None
    assert user.password_reset_expires_at is not None


async def test_request_returns_generic_200_for_unknown_email_and_sends_nothing(
    client, captured_emails, reset_limiter
):
    """Anti-enumeration: the response must be byte-identical to the known-
    email branch and no email goes out."""
    response = await client.post(REQUEST_PATH, json={"email": "ghost@example.com"})
    assert response.status_code == 200
    assert response.json() == {"message": GENERIC_PASSWORD_RESET_MESSAGE}
    assert not any(e["email_to"] == "ghost@example.com" for e in captured_emails)


# ---------------------------------------------------------------------------
# /password-reset/confirm
# ---------------------------------------------------------------------------


async def test_confirm_rotates_password_clears_token_bumps_marker(
    client, db_session, captured_emails, reset_limiter, user_factory
):
    user = user_factory(email="confirm@example.com")
    request_resp = await client.post(REQUEST_PATH, json={"email": "confirm@example.com"})
    assert request_resp.status_code == 200
    token = next(
        e for e in captured_emails if e["email_to"] == "confirm@example.com"
    )["token"]
    before = user.password_changed_at

    response = await client.post(
        CONFIRM_PATH, json={"token": token, "new_password": "Brand-NewPwd1"}
    )
    assert response.status_code == 200

    db_session.refresh(user)
    assert verify_password("Brand-NewPwd1", user.hashed_password)
    assert user.password_reset_token_hash is None
    assert user.password_reset_expires_at is None
    assert user.password_changed_at >= before


async def test_confirm_rejects_unknown_token_with_400(client, reset_limiter):
    response = await client.post(
        CONFIRM_PATH, json={"token": "totally-bogus", "new_password": "Brand-NewPwd1"}
    )
    assert response.status_code == 400


async def test_confirm_rejects_expired_token_with_400(
    client, db_session, captured_emails, reset_limiter, user_factory
):
    user = user_factory(email="confirm-expired@example.com")
    await client.post(REQUEST_PATH, json={"email": "confirm-expired@example.com"})
    token = next(
        e for e in captured_emails if e["email_to"] == "confirm-expired@example.com"
    )["token"]

    # Force expiry into the past.
    db_session.refresh(user)
    user.password_reset_expires_at = _utcnow_naive() - timedelta(seconds=1)
    db_session.add(user)
    db_session.commit()

    response = await client.post(
        CONFIRM_PATH, json={"token": token, "new_password": "Brand-NewPwd1"}
    )
    assert response.status_code == 400


async def test_confirm_token_is_one_shot(
    client, db_session, captured_emails, reset_limiter, user_factory
):
    """A used reset token cannot be replayed — second confirm returns 400."""
    user_factory(email="oneshot@example.com")
    await client.post(REQUEST_PATH, json={"email": "oneshot@example.com"})
    token = next(
        e for e in captured_emails if e["email_to"] == "oneshot@example.com"
    )["token"]

    first = await client.post(
        CONFIRM_PATH, json={"token": token, "new_password": "Brand-NewPwd1"}
    )
    assert first.status_code == 200

    second = await client.post(
        CONFIRM_PATH, json={"token": token, "new_password": "Brand-NewerPwd2"}
    )
    assert second.status_code == 400


# ---------------------------------------------------------------------------
# Recheck Phase 1.1 contract — failing-first via xfail(strict=True).
# ---------------------------------------------------------------------------


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Recheck Phase 1.1 not yet shipped — password-reset confirm should "
        "revoke every active refresh row for the user (open High finding from "
        "2026-05-09 audit). When the fix lands, this test will XPASS and "
        "strict=True will flag it for the marker to be removed."
    ),
)
async def test_confirm_revokes_refresh_family(
    client, db_session, captured_emails, reset_limiter, user_factory
):
    """Contract under construction: after a password reset confirms, every
    refresh row for that user must be revoked. Today the route bumps
    ``password_changed_at`` (which kills existing access tokens) but leaves
    refresh rows live — so a stolen refresh token still mints new access
    tokens after a reset.
    """
    user_factory(email="reset-revoke@example.com")

    # Login mints a refresh row.
    login = await client.post(
        LOGIN_PATH,
        data={"username": "reset-revoke@example.com", "password": DEFAULT_PASSWORD},
    )
    assert login.status_code == 200
    refresh_jti = decode_token(
        client.cookies["refresh_token"], expected_type="refresh"
    ).jti

    # Drive the reset.
    await client.post(REQUEST_PATH, json={"email": "reset-revoke@example.com"})
    token = next(
        e for e in captured_emails if e["email_to"] == "reset-revoke@example.com"
    )["token"]
    confirm = await client.post(
        CONFIRM_PATH, json={"token": token, "new_password": "Brand-NewPwd1"}
    )
    assert confirm.status_code == 200

    db_session.expire_all()
    row = crud_refresh_token.get_by_jti(db_session, jti=refresh_jti)
    assert row is not None
    assert row.revoked_at is not None, (
        "refresh row must be revoked on password-reset confirm"
    )
