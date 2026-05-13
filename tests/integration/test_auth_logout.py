"""Integration tests for ``POST /api/v1/auth/logout`` and ``/logout-all``.

``/logout`` must be idempotent — invalid or missing cookies still return
200 with the cookie cleared. ``/logout-all`` must revoke every refresh
row for the user AND bump ``password_changed_at`` so already-issued
access tokens immediately stop validating (no denylist needed).
"""

import pytest

from app.core.security import decode_token
from app.crud import refresh_token as crud_refresh_token
from tests.factories.user import DEFAULT_PASSWORD


pytestmark = pytest.mark.integration


LOGIN_PATH = "/api/v1/auth/login"
LOGOUT_PATH = "/api/v1/auth/logout"
LOGOUT_ALL_PATH = "/api/v1/auth/logout-all"
ME_PATH = "/api/v1/auth/me"


async def _login(client, email: str) -> tuple[str, str]:
    """Log in and return ``(access_token, refresh_cookie)``."""
    response = await client.post(
        LOGIN_PATH, data={"username": email, "password": DEFAULT_PASSWORD}
    )
    assert response.status_code == 200, response.text
    return response.json()["access_token"], client.cookies["refresh_token"]


# ---------------------------------------------------------------------------
# /logout
# ---------------------------------------------------------------------------


async def test_logout_revokes_current_refresh_row_and_clears_cookie(
    client, db_session, reset_limiter, user_factory
):
    user_factory(email="logout@example.com")
    _, refresh_cookie = await _login(client, "logout@example.com")
    decoded = decode_token(refresh_cookie, expected_type="refresh")

    response = await client.post(LOGOUT_PATH)
    assert response.status_code == 200

    # Cookie cleared.
    assert client.cookies.get("refresh_token") in (None, "")

    # Row revoked.
    db_session.expire_all()
    row = crud_refresh_token.get_by_jti(db_session, jti=decoded.jti)
    assert row is not None
    assert row.revoked_at is not None


async def test_logout_without_cookie_is_idempotent_200(client, reset_limiter):
    response = await client.post(LOGOUT_PATH)
    assert response.status_code == 200


async def test_logout_with_invalid_cookie_is_idempotent_200(client, reset_limiter):
    """Stale or malformed cookies must not error — clients should be able
    to call /logout blindly during sign-out UX."""
    client.cookies.set("refresh_token", "not-a-jwt", path="/")
    response = await client.post(LOGOUT_PATH)
    assert response.status_code == 200


async def test_logout_is_safe_to_call_twice(
    client, db_session, reset_limiter, user_factory
):
    user_factory(email="double@example.com")
    _, refresh_cookie = await _login(client, "double@example.com")
    decoded = decode_token(refresh_cookie, expected_type="refresh")

    first = await client.post(LOGOUT_PATH)
    second = await client.post(LOGOUT_PATH)
    assert first.status_code == 200
    assert second.status_code == 200

    db_session.expire_all()
    row = crud_refresh_token.get_by_jti(db_session, jti=decoded.jti)
    assert row is not None
    assert row.revoked_at is not None


# ---------------------------------------------------------------------------
# /logout-all
# ---------------------------------------------------------------------------


async def test_logout_all_revokes_every_refresh_row_for_user(
    client, db_session, reset_limiter, user_factory
):
    """Two logins from the same client mint two live families — both must
    be revoked by /logout-all."""
    user = user_factory(email="logout-all@example.com")

    access_token, first_cookie = await _login(client, "logout-all@example.com")
    first_jti = decode_token(first_cookie, expected_type="refresh").jti

    # Drop the cookie so the second /login mints a fresh independent family.
    client.cookies.delete("refresh_token", path="/")
    second_access, second_cookie = await _login(client, "logout-all@example.com")
    second_jti = decode_token(second_cookie, expected_type="refresh").jti

    response = await client.post(
        LOGOUT_ALL_PATH, headers={"Authorization": f"Bearer {second_access}"}
    )
    assert response.status_code == 200

    db_session.expire_all()
    for jti in (first_jti, second_jti):
        row = crud_refresh_token.get_by_jti(db_session, jti=jti)
        assert row is not None
        assert row.revoked_at is not None, f"jti={jti} should be revoked"


async def test_logout_all_bumps_password_changed_at_invalidating_access_tokens(
    client, db_session, reset_limiter, user_factory
):
    """The access token used to call /logout-all becomes invalid the moment
    the call returns: ``password_changed_at`` is bumped past its iat, so
    ``get_current_user`` rejects it."""
    user = user_factory(email="invalidate@example.com")
    access_token, _ = await _login(client, "invalidate@example.com")

    me_before = await client.get(
        ME_PATH, headers={"Authorization": f"Bearer {access_token}"}
    )
    assert me_before.status_code == 200

    logout = await client.post(
        LOGOUT_ALL_PATH, headers={"Authorization": f"Bearer {access_token}"}
    )
    assert logout.status_code == 200

    db_session.expire_all()
    db_session.refresh(user)
    assert user.password_changed_at is not None

    me_after = await client.get(
        ME_PATH, headers={"Authorization": f"Bearer {access_token}"}
    )
    assert me_after.status_code == 401, (
        "access token issued before password_changed_at must stop validating"
    )


async def test_logout_all_requires_authentication(client, reset_limiter):
    response = await client.post(LOGOUT_ALL_PATH)
    assert response.status_code == 401
