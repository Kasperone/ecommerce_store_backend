"""Integration tests for ``POST /api/v1/auth/change-password`` (authenticated).

Contract:
- Wrong current password → 400.
- ``new_password == current_password`` → 400 (route checks this before
  hitting CRUDUser.change_password).
- Happy path: rotates the password, bumps ``password_changed_at`` (which
  invalidates already-issued access tokens via the iat-floor in
  ``get_current_user``).
- Recheck Phase 1.2: also revokes every active refresh row for the user.
"""

import pytest

from app.core.security import decode_token, verify_password
from app.crud import refresh_token as crud_refresh_token
from tests.factories.user import DEFAULT_PASSWORD

pytestmark = pytest.mark.integration


LOGIN_PATH = "/api/v1/auth/login"
CHANGE_PATH = "/api/v1/auth/change-password"
ME_PATH = "/api/v1/auth/me"


async def _login(client, email: str) -> str:
    response = await client.post(LOGIN_PATH, data={"username": email, "password": DEFAULT_PASSWORD})
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


# ---------------------------------------------------------------------------
# Negative paths
# ---------------------------------------------------------------------------


async def test_change_password_requires_authentication(client, reset_limiter):
    response = await client.post(
        CHANGE_PATH,
        json={"current_password": DEFAULT_PASSWORD, "new_password": "Brand-NewPwd1"},
    )
    assert response.status_code == 401


async def test_change_password_rejects_wrong_current_with_400(
    client, db_session, reset_limiter, user_factory
):
    user = user_factory(email="cp-wrong@example.com")
    access = await _login(client, "cp-wrong@example.com")
    original_hash = user.hashed_password

    response = await client.post(
        CHANGE_PATH,
        headers={"Authorization": f"Bearer {access}"},
        json={"current_password": "not-the-password", "new_password": "Brand-NewPwd1"},
    )
    assert response.status_code == 400

    db_session.refresh(user)
    assert user.hashed_password == original_hash


async def test_change_password_rejects_same_as_current_with_400(
    client, db_session, reset_limiter, user_factory
):
    user = user_factory(email="cp-same@example.com")
    access = await _login(client, "cp-same@example.com")
    original_hash = user.hashed_password

    response = await client.post(
        CHANGE_PATH,
        headers={"Authorization": f"Bearer {access}"},
        json={"current_password": DEFAULT_PASSWORD, "new_password": DEFAULT_PASSWORD},
    )
    assert response.status_code == 400

    db_session.refresh(user)
    assert user.hashed_password == original_hash


# ---------------------------------------------------------------------------
# Happy path + access-token invalidation
# ---------------------------------------------------------------------------


async def test_change_password_rotates_and_invalidates_existing_access_token(
    client, db_session, reset_limiter, user_factory
):
    user = user_factory(email="cp-ok@example.com")
    access = await _login(client, "cp-ok@example.com")

    # Sanity: token works before the change.
    me_before = await client.get(ME_PATH, headers={"Authorization": f"Bearer {access}"})
    assert me_before.status_code == 200

    response = await client.post(
        CHANGE_PATH,
        headers={"Authorization": f"Bearer {access}"},
        json={"current_password": DEFAULT_PASSWORD, "new_password": "Brand-NewPwd1"},
    )
    assert response.status_code == 200

    db_session.refresh(user)
    assert verify_password("Brand-NewPwd1", user.hashed_password)

    # The same access token must now be rejected — password_changed_at was
    # bumped past its iat by the change.
    me_after = await client.get(ME_PATH, headers={"Authorization": f"Bearer {access}"})
    assert (
        me_after.status_code == 401
    ), "access tokens issued before /change-password must stop validating"


async def test_change_password_revokes_refresh_family(
    client, db_session, reset_limiter, user_factory
):
    """After /change-password, every refresh row for that user must be
    revoked. The route bumps ``password_changed_at`` (so existing access
    tokens stop validating) AND revokes refresh rows so a stolen refresh
    token cannot mint new access tokens after the change.
    """
    user_factory(email="cp-revoke@example.com")
    access = await _login(client, "cp-revoke@example.com")
    refresh_jti = decode_token(client.cookies["refresh_token"], expected_type="refresh").jti

    response = await client.post(
        CHANGE_PATH,
        headers={"Authorization": f"Bearer {access}"},
        json={"current_password": DEFAULT_PASSWORD, "new_password": "Brand-NewPwd1"},
    )
    assert response.status_code == 200

    db_session.expire_all()
    row = crud_refresh_token.get_by_jti(db_session, jti=refresh_jti)
    assert row is not None
    assert row.revoked_at is not None, "refresh row must be revoked on /change-password"


async def test_change_password_clears_refresh_cookie(
    client, db_session, reset_limiter, user_factory
):
    """The DB row was revoked; clearing the calling browser's refresh cookie
    too keeps the next ``/refresh`` from firing against a dead row."""
    user_factory(email="cp-cookie@example.com")
    access = await _login(client, "cp-cookie@example.com")
    assert client.cookies.get("refresh_token") is not None

    response = await client.post(
        CHANGE_PATH,
        headers={"Authorization": f"Bearer {access}"},
        json={"current_password": DEFAULT_PASSWORD, "new_password": "Brand-NewPwd1"},
    )
    assert response.status_code == 200

    set_cookie = response.headers.get("set-cookie", "")
    assert "refresh_token=" in set_cookie
    assert "Max-Age=0" in set_cookie or "expires=" in set_cookie.lower()
