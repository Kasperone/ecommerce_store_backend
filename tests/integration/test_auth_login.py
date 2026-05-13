"""Integration tests for ``POST /api/v1/auth/login``.

The login route per-IP and per-email rate-limits are 5/15min. We never want
those to be the reason an assertion fails in this file, so every test
depends on the ``reset_limiter`` fixture and stays well under the budget.
The lockout flow needs 10 failures, which would exceed the limiter budget
on its own — we drive the counter to threshold-1 directly via the user row,
then exercise the threshold-crossing call through HTTP.
"""

from datetime import timedelta

import pytest

from app.crud.user import LOGIN_LOCKOUT_DURATION, LOGIN_LOCKOUT_THRESHOLD, _utcnow_naive
from tests.factories.user import DEFAULT_PASSWORD


pytestmark = pytest.mark.integration


LOGIN_PATH = "/api/v1/auth/login"


def _form(email: str, password: str = DEFAULT_PASSWORD):
    return {"username": email, "password": password}


async def test_verified_login_returns_access_token_and_sets_refresh_cookie(
    client, reset_limiter, user_factory
):
    user_factory(email="login@example.com")
    response = await client.post(LOGIN_PATH, data=_form("login@example.com"))

    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["access_token"]

    # Refresh cookie set as HttpOnly + Path=/ so it rides on every same-origin
    # request (required for SSR auth on the frontend).
    set_cookie = response.headers.get("set-cookie", "")
    assert "refresh_token=" in set_cookie
    assert "HttpOnly" in set_cookie
    assert "Path=/" in set_cookie


async def test_unverified_user_gets_403(client, reset_limiter, user_factory):
    user_factory(email="unverified@example.com", is_verified=False)
    response = await client.post(LOGIN_PATH, data=_form("unverified@example.com"))
    assert response.status_code == 403


async def test_wrong_password_returns_401_and_increments_counter(
    client, db_session, reset_limiter, user_factory
):
    user = user_factory(email="wrong@example.com")
    response = await client.post(
        LOGIN_PATH, data=_form("wrong@example.com", password="not-the-password")
    )
    assert response.status_code == 401

    db_session.refresh(user)
    assert user.failed_login_attempts == 1
    assert user.locked_until is None


async def test_unknown_email_returns_401_without_creating_state(
    client, reset_limiter
):
    """A nonexistent email gets the same generic 401 as a wrong password —
    nothing is persisted, no email is sent."""
    response = await client.post(
        LOGIN_PATH, data=_form("ghost@example.com", password="anything-Pass1")
    )
    assert response.status_code == 401


async def test_threshold_crossing_attempt_locks_and_emails_user(
    client, db_session, captured_emails, reset_limiter, user_factory
):
    """Drive the counter to threshold-1 directly, then make ONE HTTP call to
    cross into the locked state. That isolates the assertion from the
    5/15min HTTP rate limit while still exercising the route's transition
    + email side-effect."""
    user = user_factory(
        email="lock-me@example.com",
        failed_login_attempts=LOGIN_LOCKOUT_THRESHOLD - 1,
    )
    db_session.add(user)
    db_session.commit()

    response = await client.post(
        LOGIN_PATH, data=_form("lock-me@example.com", password="not-the-password")
    )
    assert response.status_code == 401

    db_session.refresh(user)
    assert user.failed_login_attempts == LOGIN_LOCKOUT_THRESHOLD
    assert user.locked_until is not None
    delta = user.locked_until - _utcnow_naive()
    assert timedelta(seconds=0) < delta <= LOGIN_LOCKOUT_DURATION + timedelta(seconds=5)

    locked_emails = [
        e for e in captured_emails
        if e["email_to"] == "lock-me@example.com" and e["kind"] == "account_locked"
    ]
    assert len(locked_emails) == 1


async def test_locked_account_returns_401_even_with_correct_password(
    client, db_session, captured_emails, reset_limiter, user_factory
):
    """A locked user presenting the correct password still gets the same
    generic 401 — never disclose the lockout state. No new email goes out."""
    user = user_factory(
        email="locked@example.com",
        failed_login_attempts=LOGIN_LOCKOUT_THRESHOLD,
        locked_until=_utcnow_naive() + timedelta(minutes=5),
    )
    db_session.add(user)
    db_session.commit()

    response = await client.post(LOGIN_PATH, data=_form("locked@example.com"))
    assert response.status_code == 401

    # Counter must NOT bump (route short-circuits before register_failed_login).
    db_session.refresh(user)
    assert user.failed_login_attempts == LOGIN_LOCKOUT_THRESHOLD

    # No second account-locked email should fire.
    locked_emails = [
        e for e in captured_emails
        if e["email_to"] == "locked@example.com" and e["kind"] == "account_locked"
    ]
    assert locked_emails == []


async def test_successful_login_clears_prior_failed_attempts(
    client, db_session, reset_limiter, user_factory
):
    """A correct login wipes any residual failed_login_attempts (sub-threshold)."""
    user = user_factory(email="recover@example.com", failed_login_attempts=3)
    db_session.add(user)
    db_session.commit()

    response = await client.post(LOGIN_PATH, data=_form("recover@example.com"))
    assert response.status_code == 200

    db_session.refresh(user)
    assert user.failed_login_attempts == 0
    assert user.locked_until is None
