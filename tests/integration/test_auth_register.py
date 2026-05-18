"""Integration tests for ``POST /api/v1/auth/register``.

The endpoint is anti-enumeration: it returns the same generic 200 body for a
new email and for one that's already registered. Which email goes out (the
verification link vs. the "already registered" notice) is the only
observable distinction, so the assertions hinge on the captured email kinds.
"""

import pytest

from app.api.v1.auth import GENERIC_REGISTRATION_MESSAGE

pytestmark = pytest.mark.integration


REGISTER_PATH = "/api/v1/auth/register"


def _payload(email: str = "new@example.com", password: str = "Str0ngPass!"):
    return {
        "email": email,
        "password": password,
        "first_name": "New",
        "last_name": "User",
    }


async def test_new_email_returns_generic_200_and_sends_verification(
    client, captured_emails, reset_limiter
):
    response = await client.post(REGISTER_PATH, json=_payload("fresh@example.com"))
    assert response.status_code == 200
    assert response.json() == {"message": GENERIC_REGISTRATION_MESSAGE}

    sent = [e for e in captured_emails if e["email_to"] == "fresh@example.com"]
    assert [e["kind"] for e in sent] == ["verification"]
    # Token is forwarded to the email helper so the user can click the link.
    assert sent[0].get("token")


async def test_duplicate_email_returns_generic_200_and_sends_already_registered(
    client, captured_emails, reset_limiter, user_factory
):
    user_factory(email="taken@example.com")
    response = await client.post(REGISTER_PATH, json=_payload("taken@example.com"))
    assert response.status_code == 200
    assert response.json() == {"message": GENERIC_REGISTRATION_MESSAGE}

    sent = [e for e in captured_emails if e["email_to"] == "taken@example.com"]
    assert [e["kind"] for e in sent] == ["already_registered"]


async def test_register_response_is_indistinguishable_for_new_vs_duplicate(
    client, reset_limiter, user_factory
):
    """Both branches must return byte-identical bodies + status to defeat
    enumeration via response shape."""
    user_factory(email="known@example.com")
    new_resp = await client.post(REGISTER_PATH, json=_payload("brand-new@example.com"))
    dup_resp = await client.post(REGISTER_PATH, json=_payload("known@example.com"))

    assert new_resp.status_code == dup_resp.status_code == 200
    assert new_resp.json() == dup_resp.json()


@pytest.mark.parametrize(
    "weak_password, reason",
    [
        ("nouppercase1", "missing uppercase"),
        ("NoDigitHere", "missing digit"),
        ("Sh0rt!", "below min length"),
    ],
)
async def test_weak_password_rejected_with_422(client, reset_limiter, weak_password, reason):
    response = await client.post(
        REGISTER_PATH, json=_payload(email="weak@example.com", password=weak_password)
    )
    assert response.status_code == 422, reason


async def test_register_rate_limit_blocks_sixth_request(client, captured_emails, reset_limiter):
    """Limit is 5/hour per IP; the 6th call is 429.

    Each call uses a distinct email so we are not also bumping the per-email
    counter (only /login is per-email rate limited).
    """
    for i in range(5):
        r = await client.post(REGISTER_PATH, json=_payload(f"rl{i}@example.com"))
        assert r.status_code == 200, f"call {i} should pass the limiter"

    sixth = await client.post(REGISTER_PATH, json=_payload("rl5@example.com"))
    assert sixth.status_code == 429
    # The 6th call is rejected by slowapi before send_verification_email runs.
    assert not any(e["email_to"] == "rl5@example.com" for e in captured_emails)
