"""Integration tests for the per-endpoint slowapi rate limits.

The limiter is keyed off ``request.client.host`` for IP-based limits and
the stashed email for per-email limits. Under ASGI the test client's IP
is the constant ``"testclient"`` — that is fine for asserting "Nth call
exceeds the budget", which is the contract under test here. Where a
limit applies per email, we exhaust the same email's budget rather than
varying IPs.

Every test depends on ``reset_limiter`` to start each scenario with a
clean budget and to wipe it on teardown.

The /register IP-based limit and /login lockout interactions are already
covered in their dedicated files; here we focus on the email-keyed
limits which are not exercised elsewhere.
"""

import pytest

from tests.factories.user import DEFAULT_PASSWORD


pytestmark = [pytest.mark.integration, pytest.mark.rate_limit]


LOGIN_PATH = "/api/v1/auth/login"
RESEND_PATH = "/api/v1/auth/resend-verification"
RESET_REQUEST_PATH = "/api/v1/auth/password-reset/request"


# ---------------------------------------------------------------------------
# /login — 5/15min per IP and per email.
# ---------------------------------------------------------------------------


async def test_login_per_ip_limit_exhausts_at_sixth_attempt(
    client, reset_limiter, user_factory
):
    """Five wrong-password attempts share the IP budget; the 6th call —
    even with a fresh email — must hit the per-IP limit and return 429."""
    for i in range(5):
        user_factory(email=f"rl-ip{i}@example.com")
        r = await client.post(
            LOGIN_PATH,
            data={"username": f"rl-ip{i}@example.com", "password": "wrong-password"},
        )
        assert r.status_code == 401, f"call {i} should pass through to 401"

    user_factory(email="rl-ip5@example.com")
    sixth = await client.post(
        LOGIN_PATH,
        data={"username": "rl-ip5@example.com", "password": "wrong-password"},
    )
    assert sixth.status_code == 429


async def test_login_per_email_limit_exhausts_at_sixth_attempt(
    client, reset_limiter, user_factory
):
    """Five wrong-password attempts against the SAME email exhaust the
    per-email budget. The 6th call returns 429 rather than 401."""
    user_factory(email="rl-email@example.com")
    for i in range(5):
        r = await client.post(
            LOGIN_PATH,
            data={"username": "rl-email@example.com", "password": "wrong-password"},
        )
        assert r.status_code == 401, f"call {i} should pass through to 401"

    sixth = await client.post(
        LOGIN_PATH,
        data={"username": "rl-email@example.com", "password": DEFAULT_PASSWORD},
    )
    assert sixth.status_code == 429, (
        "per-email limit must fire even when the 6th attempt has the correct password"
    )


# ---------------------------------------------------------------------------
# /resend-verification — 3/hour per email.
# ---------------------------------------------------------------------------


async def test_resend_verification_per_email_limit_blocks_fourth_request(
    client, reset_limiter, user_factory
):
    user_factory(email="rl-resend@example.com", is_verified=False)
    for i in range(3):
        r = await client.post(RESEND_PATH, json={"email": "rl-resend@example.com"})
        assert r.status_code == 200, f"call {i} should pass the limiter"

    fourth = await client.post(RESEND_PATH, json={"email": "rl-resend@example.com"})
    assert fourth.status_code == 429


# ---------------------------------------------------------------------------
# /password-reset/request — 3/hour per email.
# ---------------------------------------------------------------------------


async def test_password_reset_request_per_email_limit_blocks_fourth_request(
    client, reset_limiter, user_factory
):
    user_factory(email="rl-reset@example.com")
    for i in range(3):
        r = await client.post(
            RESET_REQUEST_PATH, json={"email": "rl-reset@example.com"}
        )
        assert r.status_code == 200, f"call {i} should pass the limiter"

    fourth = await client.post(
        RESET_REQUEST_PATH, json={"email": "rl-reset@example.com"}
    )
    assert fourth.status_code == 429


async def test_password_reset_request_per_email_limit_isolates_buckets_by_email(
    client, reset_limiter, user_factory
):
    """Exhausting one email's budget must not affect a different email."""
    user_factory(email="rl-bucket-a@example.com")
    user_factory(email="rl-bucket-b@example.com")

    for _ in range(3):
        r = await client.post(
            RESET_REQUEST_PATH, json={"email": "rl-bucket-a@example.com"}
        )
        assert r.status_code == 200

    blocked = await client.post(
        RESET_REQUEST_PATH, json={"email": "rl-bucket-a@example.com"}
    )
    assert blocked.status_code == 429

    other = await client.post(
        RESET_REQUEST_PATH, json={"email": "rl-bucket-b@example.com"}
    )
    assert other.status_code == 200, "different email must have its own budget"
