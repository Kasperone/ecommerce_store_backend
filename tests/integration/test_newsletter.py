"""Integration tests for the public newsletter subscribe endpoint.

``POST /api/v1/newsletter/subscribe`` is idempotent and anti-enumeration: it
always returns a generic 200 ``{"status": "subscribed"}`` whether the email is
new, already subscribed, or reactivated. A re-subscribe reactivates the single
row rather than creating a duplicate. When the email maps to a registered user
a ``user_consents`` newsletter row is appended; for an anonymous email the
subscriber row itself captures the opt-in (with ip/ua/source).

The rate-limit test follows the ``test_rate_limits`` pattern (``reset_limiter``,
exhaust the per-email budget, assert 429 on the over-cap call).
"""

from __future__ import annotations

import pytest

from app.models.newsletter_subscriber import NewsletterSubscriber
from app.models.user_consent import UserConsent

SUBSCRIBE_PATH = "/api/v1/newsletter/subscribe"

pytestmark = pytest.mark.integration


async def test_subscribe_new_email_returns_generic_200_and_creates_row(client, db_session):
    resp = await client.post(SUBSCRIBE_PATH, json={"email": "fresh@example.com"})
    assert resp.status_code == 200
    assert resp.json() == {"status": "subscribed"}

    row = (
        db_session.query(NewsletterSubscriber)
        .filter(NewsletterSubscriber.email == "fresh@example.com")
        .one()
    )
    assert row.is_active is True
    assert row.source == "footer"
    assert row.unsubscribe_token  # generated, non-empty
    assert row.confirmed_at is None  # reserved slot, never set this wave


async def test_subscribe_is_idempotent_no_duplicate_row(client, db_session):
    """Re-subscribing the same email returns 200 again and keeps a single row."""
    first = await client.post(SUBSCRIBE_PATH, json={"email": "dup@example.com"})
    assert first.status_code == 200

    second = await client.post(SUBSCRIBE_PATH, json={"email": "dup@example.com"})
    assert second.status_code == 200
    assert second.json() == {"status": "subscribed"}  # no 409, no state leak

    rows = (
        db_session.query(NewsletterSubscriber)
        .filter(NewsletterSubscriber.email == "dup@example.com")
        .all()
    )
    assert len(rows) == 1


async def test_resubscribe_reactivates_existing_row(client, db_session):
    """A parked (inactive) subscriber is reactivated, not duplicated."""
    await client.post(SUBSCRIBE_PATH, json={"email": "reactivate@example.com"})

    row = (
        db_session.query(NewsletterSubscriber)
        .filter(NewsletterSubscriber.email == "reactivate@example.com")
        .one()
    )
    row.is_active = False
    db_session.commit()

    resp = await client.post(SUBSCRIBE_PATH, json={"email": "reactivate@example.com"})
    assert resp.status_code == 200

    db_session.refresh(row)
    assert row.is_active is True


async def test_email_is_normalized_lowercased(client, db_session):
    """Mixed-case / padded email is stored lowercased; a casing variant is the
    same subscriber (no duplicate)."""
    await client.post(SUBSCRIBE_PATH, json={"email": "Mixed@Example.com"})
    await client.post(SUBSCRIBE_PATH, json={"email": "mixed@example.com"})

    rows = (
        db_session.query(NewsletterSubscriber)
        .filter(NewsletterSubscriber.email == "mixed@example.com")
        .all()
    )
    assert len(rows) == 1


async def test_subscribe_existing_user_appends_consent(client, db_session, user_factory):
    """When the email belongs to a registered user, a `user_consents`
    newsletter row is appended for the GDPR audit trail."""
    user = user_factory(email="member@example.com")

    resp = await client.post(SUBSCRIBE_PATH, json={"email": "member@example.com"})
    assert resp.status_code == 200

    consents = (
        db_session.query(UserConsent)
        .filter(
            UserConsent.user_id == user.id,
            UserConsent.consent_type == "newsletter",
        )
        .all()
    )
    assert len(consents) == 1
    assert consents[0].granted is True
    assert consents[0].source == "newsletter_signup"


async def test_subscribe_anonymous_email_records_no_consent(client, db_session):
    """An email with no account leaves no `user_consents` row — the subscriber
    row IS the consent record."""
    await client.post(SUBSCRIBE_PATH, json={"email": "anon@example.com"})

    consents = db_session.query(UserConsent).all()
    assert consents == []

    row = (
        db_session.query(NewsletterSubscriber)
        .filter(NewsletterSubscriber.email == "anon@example.com")
        .one()
    )
    assert row.is_active is True


async def test_subscribe_rejects_invalid_email(client):
    resp = await client.post(SUBSCRIBE_PATH, json={"email": "not-an-email"})
    assert resp.status_code == 422


@pytest.mark.rate_limit
async def test_subscribe_per_email_rate_limited(client, reset_limiter):
    """The 10/hour per-email budget is exhausted, then the 11th call returns 429."""
    for i in range(10):
        r = await client.post(SUBSCRIBE_PATH, json={"email": "rl-news@example.com"})
        assert r.status_code == 200, f"call {i} should pass the limiter"

    over = await client.post(SUBSCRIBE_PATH, json={"email": "rl-news@example.com"})
    assert over.status_code == 429
