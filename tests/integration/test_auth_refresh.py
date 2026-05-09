"""Integration tests for ``POST /api/v1/auth/refresh`` (rotation +
reuse detection).

Each test logs in to obtain a real refresh cookie, then exercises the
rotation behaviour. The reuse-detection test replays an already-rotated
cookie and asserts the entire family for that user is revoked.
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


async def test_refresh_reuse_detection_revokes_family(
    client, db_session, reset_limiter, user_factory
):
    """Replaying an already-rotated refresh cookie must:
    - return 401
    - revoke EVERY active refresh row for that user (token theft signal —
      the legitimate client would be holding the rotation replacement).

    Setup: log in twice from the same client to mint two live families;
    capture the first cookie, rotate it once (so it's now revoked),
    then replay it.
    """
    user = user_factory(email="reuse@example.com")

    # Family A — login → rotate once so the first cookie is now revoked.
    family_a_first = await _login(client, "reuse@example.com")
    family_a_first_jti = decode_token(family_a_first, expected_type="refresh").jti
    rotate = await client.post(REFRESH_PATH)
    assert rotate.status_code == 200
    family_a_second = client.cookies.get("refresh_token")
    family_a_second_jti = decode_token(family_a_second, expected_type="refresh").jti

    # Family B — second login mints an independent live row that should
    # also be wiped by reuse-detection.
    client.cookies.delete("refresh_token", path="/api/v1/auth")
    family_b = await _login(client, "reuse@example.com")
    family_b_jti = decode_token(family_b, expected_type="refresh").jti

    # Replay the revoked cookie.
    client.cookies.set("refresh_token", family_a_first, path="/api/v1/auth")
    response = await client.post(REFRESH_PATH)
    assert response.status_code == 401

    # Every refresh row for this user must now be revoked.
    db_session.expire_all()
    for jti in (family_a_first_jti, family_a_second_jti, family_b_jti):
        row = crud_refresh_token.get_by_jti(db_session, jti=jti)
        assert row is not None, f"row for jti={jti} should still exist"
        assert row.revoked_at is not None, (
            f"reuse-detection must revoke jti={jti} (user_id={user.id})"
        )


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
