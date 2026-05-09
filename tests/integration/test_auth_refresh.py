"""Integration tests for ``POST /api/v1/auth/refresh`` (rotation).

Each test logs in to obtain a real refresh cookie, then exercises the
rotation behaviour. Reuse-detection (replay of a revoked token) is left
for 1.4.a; this file covers the happy path and the three rejection
shapes called out in the plan.
"""

import pytest

from app.core.security import decode_token
from app.crud import refresh_token as crud_refresh_token
from tests.factories.user import DEFAULT_PASSWORD


pytestmark = pytest.mark.integration


LOGIN_PATH = "/api/v1/auth/login"
REFRESH_PATH = "/api/v1/auth/refresh"


async def _login(client, email: str) -> str:
    """Log in and return the resulting refresh cookie value."""
    response = await client.post(
        LOGIN_PATH, data={"username": email, "password": DEFAULT_PASSWORD}
    )
    assert response.status_code == 200, response.text
    cookie = client.cookies.get("refresh_token")
    assert cookie, "login must set refresh_token cookie"
    return cookie


async def test_refresh_rotates_token_and_revokes_old_row(
    client, db_session, reset_limiter, user_factory
):
    user = user_factory(email="rotate@example.com")
    old_cookie = await _login(client, "rotate@example.com")
    old_decoded = decode_token(old_cookie, expected_type="refresh")

    response = await client.post(REFRESH_PATH)
    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["access_token"]

    # New cookie should be issued and differ from the old one.
    new_cookie = client.cookies.get("refresh_token")
    assert new_cookie, "rotation must set a new refresh_token cookie"
    assert new_cookie != old_cookie

    new_decoded = decode_token(new_cookie, expected_type="refresh")
    assert new_decoded.jti != old_decoded.jti
    assert new_decoded.subject == str(user.id)

    # Old row must be revoked, with replaced_by_jti pointing at the new row.
    db_session.expire_all()
    old_row = crud_refresh_token.get_by_jti(db_session, jti=old_decoded.jti)
    assert old_row is not None
    assert old_row.revoked_at is not None
    assert old_row.replaced_by_jti == new_decoded.jti

    # New row must exist and be live.
    new_row = crud_refresh_token.get_by_jti(db_session, jti=new_decoded.jti)
    assert new_row is not None
    assert new_row.revoked_at is None
    assert new_row.user_id == user.id


async def test_refresh_without_cookie_returns_401(client, reset_limiter):
    response = await client.post(REFRESH_PATH)
    assert response.status_code == 401


async def test_refresh_with_tampered_cookie_returns_401(
    client, reset_limiter, user_factory
):
    user_factory(email="tamper@example.com")
    cookie = await _login(client, "tamper@example.com")

    # Flip the last character of the signature segment so the JWT no longer
    # validates. The malformed cookie must not be accepted by /refresh.
    head, _, sig = cookie.rpartition(".")
    bad_char = "A" if sig[-1] != "A" else "B"
    tampered = f"{head}.{sig[:-1]}{bad_char}"
    client.cookies.set("refresh_token", tampered, path="/api/v1/auth")

    response = await client.post(REFRESH_PATH)
    assert response.status_code == 401


async def test_refresh_with_expired_db_row_returns_401(
    client, db_session, reset_limiter, user_factory
):
    """JWT can still validate cryptographically while the persisted row's
    expires_at is already in the past — the route must reject on row state."""
    from datetime import timedelta

    from app.crud.user import _utcnow_naive

    user_factory(email="row-expired@example.com")
    cookie = await _login(client, "row-expired@example.com")
    decoded = decode_token(cookie, expected_type="refresh")

    row = crud_refresh_token.get_by_jti(db_session, jti=decoded.jti)
    assert row is not None
    row.expires_at = _utcnow_naive() - timedelta(seconds=1)
    db_session.add(row)
    db_session.commit()

    response = await client.post(REFRESH_PATH)
    assert response.status_code == 401

    # Expired row gets revoked as a side effect (route calls revoke()).
    db_session.expire_all()
    row = crud_refresh_token.get_by_jti(db_session, jti=decoded.jti)
    assert row is not None
    assert row.revoked_at is not None
