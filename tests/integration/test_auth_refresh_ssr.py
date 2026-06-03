"""Integration tests for ``POST /api/v1/auth/refresh/ssr`` — the server-to-server
SSR session-validation endpoint.

Unlike ``/refresh`` this path is **read-only**: gated by ``SSR_SHARED_SECRET``,
it validates the refresh cookie and mints an access token WITHOUT rotating it,
without a ``Set-Cookie``, and without triggering family-wide reuse-detection. It
exists to let the Nuxt SSR server resolve the visitor on every cold load without
burning the rotation/Set-Cookie-replay race (a hydration-mismatch source) or the
user-facing 30/hour refresh limit.
"""

import pytest

from app.core.config import settings
from app.core.security import decode_token
from app.crud import refresh_token as crud_refresh_token
from tests.factories.user import DEFAULT_PASSWORD

pytestmark = pytest.mark.integration


LOGIN_PATH = "/api/v1/auth/login"
REFRESH_PATH = "/api/v1/auth/refresh"
SSR_PATH = "/api/v1/auth/refresh/ssr"
SSR_SECRET = "test-ssr-shared-secret-value"


@pytest.fixture
def ssr_headers(monkeypatch):
    """Configure a known SSR shared secret and return the matching header."""
    monkeypatch.setattr(settings, "SSR_SHARED_SECRET", SSR_SECRET)
    return {"X-SSR-Secret": SSR_SECRET}


async def _login(client, email: str) -> str:
    """Log in and return the resulting refresh cookie value."""
    response = await client.post(LOGIN_PATH, data={"username": email, "password": DEFAULT_PASSWORD})
    assert response.status_code == 200, response.text
    cookie = client.cookies.get("refresh_token")
    assert cookie, "login must set refresh_token cookie"
    return cookie


async def test_ssr_refresh_mints_access_token_without_rotating(
    client, db_session, reset_limiter, user_factory, ssr_headers
):
    """Happy path: a valid cookie yields a fresh access token, and the refresh
    token is left completely untouched — no new cookie, no DB rotation."""
    user = user_factory(email="ssr-ok@example.com")
    cookie = await _login(client, "ssr-ok@example.com")
    decoded = decode_token(cookie, expected_type="refresh")

    response = await client.post(SSR_PATH, headers=ssr_headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["token_type"] == "bearer"

    # Minted token is a real access token for this user.
    access = decode_token(body["access_token"], expected_type="access")
    assert access.subject == str(user.id)

    # No rotation: no Set-Cookie on the response, and the original cookie is
    # unchanged and still live in the DB (not revoked, not replaced).
    assert "set-cookie" not in response.headers
    assert client.cookies.get("refresh_token") == cookie
    db_session.expire_all()
    row = crud_refresh_token.get_by_jti(db_session, jti=decoded.jti)
    assert row is not None
    assert row.revoked_at is None, "SSR validation must not revoke the refresh row"
    assert row.replaced_by_jti is None, "SSR validation must not rotate the refresh row"


async def test_ssr_refresh_without_secret_header_returns_403(
    client, reset_limiter, user_factory, ssr_headers
):
    """A configured secret but no header ⇒ 403 (ordinary clients can't reach it)."""
    user_factory(email="ssr-nohdr@example.com")
    await _login(client, "ssr-nohdr@example.com")
    response = await client.post(SSR_PATH)  # no X-SSR-Secret
    assert response.status_code == 403


async def test_ssr_refresh_with_wrong_secret_returns_403(
    client, reset_limiter, user_factory, ssr_headers
):
    user_factory(email="ssr-wrong@example.com")
    await _login(client, "ssr-wrong@example.com")
    response = await client.post(SSR_PATH, headers={"X-SSR-Secret": "not-the-secret"})
    assert response.status_code == 403


async def test_ssr_refresh_disabled_when_secret_unset_returns_403(
    client, db_session, reset_limiter, user_factory, monkeypatch
):
    """Empty ``SSR_SHARED_SECRET`` fails closed: the endpoint 403s even when a
    header is supplied, so an unconfigured environment can't expose it."""
    monkeypatch.setattr(settings, "SSR_SHARED_SECRET", "")
    user_factory(email="ssr-disabled@example.com")
    await _login(client, "ssr-disabled@example.com")
    response = await client.post(SSR_PATH, headers={"X-SSR-Secret": ""})
    assert response.status_code == 403


async def test_ssr_refresh_without_cookie_returns_401(client, reset_limiter, ssr_headers):
    """Valid secret, no cookie ⇒ 401 (gate passed, nothing to validate)."""
    response = await client.post(SSR_PATH, headers=ssr_headers)
    assert response.status_code == 401


async def test_ssr_refresh_with_tampered_cookie_returns_401(
    client, reset_limiter, user_factory, ssr_headers
):
    user_factory(email="ssr-tamper@example.com")
    cookie = await _login(client, "ssr-tamper@example.com")
    head, _, sig = cookie.rpartition(".")
    bad_char = "A" if sig[-1] != "A" else "B"
    client.cookies.set("refresh_token", f"{head}.{sig[:-1]}{bad_char}", path="/")
    response = await client.post(SSR_PATH, headers=ssr_headers)
    assert response.status_code == 401


async def test_ssr_refresh_revoked_cookie_returns_401_without_family_revocation(
    client, db_session, reset_limiter, user_factory, ssr_headers
):
    """The key contrast with ``/refresh``: presenting an already-rotated
    (revoked) cookie to the SSR path returns 401 but must NOT revoke the rest of
    the family — at SSR time a stale cookie is a benign rotation race, not theft,
    and nuking the family would log the user out everywhere."""
    user_factory(email="ssr-revoked@example.com")
    first_cookie = await _login(client, "ssr-revoked@example.com")
    first_jti = decode_token(first_cookie, expected_type="refresh").jti

    # Rotate once via the real /refresh so `first_cookie` is now revoked and a
    # live replacement exists.
    rotate = await client.post(REFRESH_PATH)
    assert rotate.status_code == 200
    live_cookie = client.cookies.get("refresh_token")
    live_jti = decode_token(live_cookie, expected_type="refresh").jti

    # Replay the revoked cookie against the SSR path.
    client.cookies.set("refresh_token", first_cookie, path="/")
    response = await client.post(SSR_PATH, headers=ssr_headers)
    assert response.status_code == 401

    # The live replacement row must remain usable — no family-wide revocation.
    db_session.expire_all()
    live_row = crud_refresh_token.get_by_jti(db_session, jti=live_jti)
    assert live_row is not None
    assert live_row.revoked_at is None, "SSR path must not revoke the live family member"
    # The replayed row is of course still revoked (from the earlier rotation).
    first_row = crud_refresh_token.get_by_jti(db_session, jti=first_jti)
    assert first_row is not None and first_row.revoked_at is not None


async def test_ssr_refresh_with_expired_row_returns_401_without_revoking(
    client, db_session, reset_limiter, user_factory, ssr_headers
):
    """An expired DB row ⇒ 401. Unlike ``/refresh`` (which revokes expired rows
    as a side effect) the read-only SSR path leaves the row untouched."""
    from datetime import timedelta

    from app.crud.user import _utcnow_naive

    user_factory(email="ssr-expired@example.com")
    cookie = await _login(client, "ssr-expired@example.com")
    decoded = decode_token(cookie, expected_type="refresh")

    row = crud_refresh_token.get_by_jti(db_session, jti=decoded.jti)
    assert row is not None
    row.expires_at = _utcnow_naive() - timedelta(seconds=1)
    db_session.add(row)
    db_session.commit()

    response = await client.post(SSR_PATH, headers=ssr_headers)
    assert response.status_code == 401

    db_session.expire_all()
    row = crud_refresh_token.get_by_jti(db_session, jti=decoded.jti)
    assert row is not None
    assert row.revoked_at is None, "read-only SSR path must not revoke the expired row"


async def test_ssr_refresh_inactive_user_returns_401(
    client, db_session, reset_limiter, user_factory, ssr_headers
):
    user = user_factory(email="ssr-inactive@example.com")
    await _login(client, "ssr-inactive@example.com")
    user.is_active = False
    db_session.add(user)
    db_session.commit()
    response = await client.post(SSR_PATH, headers=ssr_headers)
    assert response.status_code == 401


async def test_ssr_refresh_endpoint_hidden_from_openapi(client):
    """Internal server-to-server route — must not appear in the public schema."""
    schema = (await client.get("/openapi.json")).json()
    assert "/api/v1/auth/refresh/ssr" not in schema["paths"]
    assert "/api/v1/auth/refresh" in schema["paths"]
