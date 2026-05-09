"""Integration tests for ``POST /api/v1/auth/verify-email``.

Contract:
- Valid token → flips ``is_verified=True``, deletes the token row,
  sends a welcome email.
- Already-verified user with a still-live token → 200 short-circuit,
  no welcome email re-sent.
- Replaying a consumed token → 400 (row was deleted).
- Expired token → 400.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.crud.verification_token import verification_token


pytestmark = pytest.mark.integration


VERIFY_PATH = "/api/v1/auth/verify-email"


async def test_verify_email_flips_is_verified_and_deletes_token(
    client, db_session, captured_emails, reset_limiter, user_factory
):
    user = user_factory(email="verify@example.com", is_verified=False)
    token_row = verification_token.create_for_user(db_session, user_id=user.id)
    token_str = token_row.token

    response = await client.post(VERIFY_PATH, json={"token": token_str})
    assert response.status_code == 200
    assert response.json() == {"message": "Email verified successfully"}

    db_session.refresh(user)
    assert user.is_verified is True

    # Token row consumed (deleted) — replay must fail.
    db_session.expire_all()
    assert verification_token.get_by_token(db_session, token=token_str) is None

    welcomes = [
        e for e in captured_emails
        if e["email_to"] == "verify@example.com" and e["kind"] == "welcome"
    ]
    assert len(welcomes) == 1


async def test_verify_email_short_circuits_when_already_verified(
    client, db_session, captured_emails, reset_limiter, user_factory
):
    """If the user is already verified the route returns the short-circuit
    message and does NOT send a second welcome email."""
    user = user_factory(email="already@example.com", is_verified=True)
    token_row = verification_token.create_for_user(db_session, user_id=user.id)

    response = await client.post(VERIFY_PATH, json={"token": token_row.token})
    assert response.status_code == 200
    assert response.json() == {"message": "Email already verified"}

    welcomes = [
        e for e in captured_emails
        if e["email_to"] == "already@example.com" and e["kind"] == "welcome"
    ]
    assert welcomes == []


async def test_verify_email_replay_returns_400(
    client, db_session, reset_limiter, user_factory
):
    """A consumed token cannot be replayed — the row is deleted on first use
    so the second call must hit the generic 'invalid token' branch."""
    user = user_factory(email="replay@example.com", is_verified=False)
    token_row = verification_token.create_for_user(db_session, user_id=user.id)
    token_str = token_row.token

    first = await client.post(VERIFY_PATH, json={"token": token_str})
    assert first.status_code == 200

    second = await client.post(VERIFY_PATH, json={"token": token_str})
    assert second.status_code == 400


async def test_verify_email_rejects_expired_token_with_400(
    client, db_session, reset_limiter, user_factory
):
    user = user_factory(email="exp@example.com", is_verified=False)
    token_row = verification_token.create_for_user(db_session, user_id=user.id)

    # Force expiry into the past. ``expires_at`` is stored as tz-aware UTC
    # for verification tokens, so use the same shape.
    token_row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db_session.add(token_row)
    db_session.commit()

    response = await client.post(VERIFY_PATH, json={"token": token_row.token})
    assert response.status_code == 400

    db_session.refresh(user)
    assert user.is_verified is False


async def test_verify_email_rejects_unknown_token_with_400(client, reset_limiter):
    response = await client.post(VERIFY_PATH, json={"token": "totally-bogus-token"})
    assert response.status_code == 400
