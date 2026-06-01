"""Integration tests for ``GET /api/v1/auth/sessions`` and
``DELETE /api/v1/auth/sessions/{id}``.

Sessions ARE ``refresh_tokens`` rows. The list returns only the caller's
active sessions, newest-first, flagging the row whose ``jti`` matches the
caller's refresh cookie as ``current``. Revoking is owner-gated: cross-user,
unknown, or already-revoked ids all return 404 (never 403) to avoid
enumeration.
"""

import pytest

from app.core.security import decode_token
from app.crud import refresh_token as crud_refresh_token
from tests.factories.user import DEFAULT_PASSWORD

pytestmark = pytest.mark.integration


LOGIN_PATH = "/api/v1/auth/login"
SESSIONS_PATH = "/api/v1/auth/sessions"


async def _login(client, email: str) -> tuple[str, str]:
    """Log in and return ``(access_token, refresh_cookie)``."""
    response = await client.post(LOGIN_PATH, data={"username": email, "password": DEFAULT_PASSWORD})
    assert response.status_code == 200, response.text
    return response.json()["access_token"], client.cookies["refresh_token"]


# ---------------------------------------------------------------------------
# GET /sessions
# ---------------------------------------------------------------------------


async def test_list_sessions_requires_authentication(client, reset_limiter):
    response = await client.get(SESSIONS_PATH)
    assert response.status_code == 401


async def test_list_sessions_returns_only_active_rows(
    client, db_session, reset_limiter, user_factory
):
    user_factory(email="list-active@example.com")
    access, cookie = await _login(client, "list-active@example.com")
    first_jti = decode_token(cookie, expected_type="refresh").jti

    # Second login → second active family.
    client.cookies.delete("refresh_token", path="/")
    await _login(client, "list-active@example.com")

    # Revoke the first family directly; it must drop out of the list.
    row = crud_refresh_token.get_by_jti(db_session, jti=first_jti)
    assert row is not None
    crud_refresh_token.revoke(db_session, row=row)

    response = await client.get(SESSIONS_PATH, headers={"Authorization": f"Bearer {access}"})
    assert response.status_code == 200
    body = response.json()
    assert all(s["id"] != row.id for s in body), "revoked session must not be listed"
    assert len(body) == 1


async def test_list_sessions_marks_current_via_cookie(
    client, db_session, reset_limiter, user_factory
):
    user_factory(email="current-flag@example.com")
    access, cookie = await _login(client, "current-flag@example.com")
    current_jti = decode_token(cookie, expected_type="refresh").jti
    current_row = crud_refresh_token.get_by_jti(db_session, jti=current_jti)
    assert current_row is not None

    # A second family with no matching cookie.
    client.cookies.delete("refresh_token", path="/")
    await _login(client, "current-flag@example.com")
    # Restore the first cookie so the request carries it.
    client.cookies.set("refresh_token", cookie, path="/")

    response = await client.get(SESSIONS_PATH, headers={"Authorization": f"Bearer {access}"})
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 2

    current = [s for s in body if s["current"]]
    assert len(current) == 1
    # Exactly the cookie's row is flagged current.
    assert current[0]["id"] == current_row.id
    assert sum(1 for s in body if not s["current"]) == 1


async def test_list_sessions_no_cookie_marks_none_current(client, reset_limiter, user_factory):
    user_factory(email="no-cookie@example.com")
    access, _cookie = await _login(client, "no-cookie@example.com")
    # Drop the cookie before listing.
    client.cookies.delete("refresh_token", path="/")

    response = await client.get(SESSIONS_PATH, headers={"Authorization": f"Bearer {access}"})
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["current"] is False


async def test_list_sessions_is_user_scoped(client, reset_limiter, user_factory):
    user_factory(email="scoped-a@example.com")
    user_factory(email="scoped-b@example.com")

    # User B logs in (creates a session) — must be invisible to A.
    await _login(client, "scoped-b@example.com")
    client.cookies.delete("refresh_token", path="/")

    access_a, _cookie_a = await _login(client, "scoped-a@example.com")
    response = await client.get(SESSIONS_PATH, headers={"Authorization": f"Bearer {access_a}"})
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1  # only A's own session


async def test_list_sessions_shape(client, reset_limiter, user_factory):
    user_factory(email="shape@example.com")
    access, _cookie = await _login(client, "shape@example.com")
    response = await client.get(SESSIONS_PATH, headers={"Authorization": f"Bearer {access}"})
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    session = body[0]
    assert set(session.keys()) == {
        "id",
        "created_at",
        "created_ip",
        "user_agent",
        "current",
        "expires_at",
    }
    assert isinstance(session["id"], int)
    assert isinstance(session["current"], bool)


# ---------------------------------------------------------------------------
# DELETE /sessions/{id}
# ---------------------------------------------------------------------------


async def test_delete_own_active_session_204_and_revoked(
    client, db_session, reset_limiter, user_factory
):
    user_factory(email="del-own@example.com")
    access, cookie = await _login(client, "del-own@example.com")
    jti = decode_token(cookie, expected_type="refresh").jti
    row = crud_refresh_token.get_by_jti(db_session, jti=jti)
    assert row is not None

    response = await client.delete(
        f"{SESSIONS_PATH}/{row.id}", headers={"Authorization": f"Bearer {access}"}
    )
    assert response.status_code == 204

    db_session.expire_all()
    refreshed = crud_refresh_token.get(db_session, id=row.id)
    assert refreshed is not None
    assert refreshed.revoked_at is not None


async def test_delete_does_not_clear_cookie(client, db_session, reset_limiter, user_factory):
    user_factory(email="del-cookie@example.com")
    access, cookie = await _login(client, "del-cookie@example.com")
    jti = decode_token(cookie, expected_type="refresh").jti
    row = crud_refresh_token.get_by_jti(db_session, jti=jti)
    assert row is not None

    response = await client.delete(
        f"{SESSIONS_PATH}/{row.id}", headers={"Authorization": f"Bearer {access}"}
    )
    assert response.status_code == 204
    # Revoking a session must NOT clear the refresh cookie — that's /logout's job.
    assert client.cookies.get("refresh_token") == cookie


async def test_delete_another_users_session_404(client, db_session, reset_limiter, user_factory):
    user_factory(email="victim@example.com")
    user_factory(email="attacker@example.com")

    # Victim logs in → owns a session row.
    _victim_access, victim_cookie = await _login(client, "victim@example.com")
    victim_jti = decode_token(victim_cookie, expected_type="refresh").jti
    victim_row = crud_refresh_token.get_by_jti(db_session, jti=victim_jti)
    assert victim_row is not None

    client.cookies.delete("refresh_token", path="/")
    attacker_access, _ = await _login(client, "attacker@example.com")

    response = await client.delete(
        f"{SESSIONS_PATH}/{victim_row.id}",
        headers={"Authorization": f"Bearer {attacker_access}"},
    )
    assert response.status_code == 404

    # Victim's row is untouched.
    db_session.expire_all()
    still = crud_refresh_token.get(db_session, id=victim_row.id)
    assert still is not None
    assert still.revoked_at is None


async def test_delete_unknown_id_404(client, reset_limiter, user_factory):
    user_factory(email="unknown@example.com")
    access, _cookie = await _login(client, "unknown@example.com")
    response = await client.delete(
        f"{SESSIONS_PATH}/999999", headers={"Authorization": f"Bearer {access}"}
    )
    assert response.status_code == 404


async def test_delete_already_revoked_session_404(client, db_session, reset_limiter, user_factory):
    user_factory(email="del-twice@example.com")
    access, cookie = await _login(client, "del-twice@example.com")
    jti = decode_token(cookie, expected_type="refresh").jti
    row = crud_refresh_token.get_by_jti(db_session, jti=jti)
    assert row is not None
    crud_refresh_token.revoke(db_session, row=row)

    response = await client.delete(
        f"{SESSIONS_PATH}/{row.id}", headers={"Authorization": f"Bearer {access}"}
    )
    assert response.status_code == 404


async def test_delete_requires_authentication(client, reset_limiter):
    response = await client.delete(f"{SESSIONS_PATH}/1")
    assert response.status_code == 401
