"""Integration tests for the public newsletter subscribe endpoint.

``POST /api/v1/newsletter/subscribe`` is idempotent and anti-enumeration: it
always returns a generic 200 ``{"status": "subscribed"}`` whether the email is
new, already subscribed, or reactivated. A re-subscribe reactivates the single
row rather than creating a duplicate. When the email maps to a registered user
a ``user_consents`` newsletter row is appended; for an anonymous email the
subscriber row itself captures the opt-in (with ip/ua/source).

The rate-limit test follows the ``test_rate_limits`` pattern (``reset_limiter``,
exhaust the per-email budget, assert 429 on the over-cap call).

``GET/POST /api/v1/newsletter/unsubscribe`` is the opt-out counterpart: a signed
token whose subject is the address, so it works for registered users, company
contacts and anonymous subscribers alike.
"""

from __future__ import annotations

import pytest

from app.core.security import create_access_token, create_unsubscribe_token
from app.crud import email_suppression as crud_email_suppression
from app.crud import user_consent as crud_user_consent
from app.models.email_suppression import EmailSuppression
from app.models.newsletter_subscriber import NewsletterSubscriber
from app.models.user import UserRole
from app.models.user_consent import UserConsent

SUBSCRIBE_PATH = "/api/v1/newsletter/subscribe"
UNSUBSCRIBE_PATH = "/api/v1/newsletter/unsubscribe"

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


# ---------------------------------------------------------------------------
# unsubscribe — the opt-out half of the marketing-consent story
# ---------------------------------------------------------------------------


async def test_unsubscribe_page_opts_out_every_channel(client, db_session, user_factory):
    """One click must stop marketing across all three recipient shapes.

    The same human can be a ``users`` row, a ``newsletter_subscribers`` row and
    a campaign recipient at once, so the route writes to all of them. Asserting
    only the suppression row would miss a stale ``granted`` consent that the
    GDPR audit trail would still show.
    """
    user = user_factory(email="unsub-all@example.com", role=UserRole.CUSTOMER)
    crud_user_consent.record(
        db_session, user_id=user.id, consent_type="marketing_email", granted=True, source="test"
    )
    db_session.commit()
    await client.post(SUBSCRIBE_PATH, json={"email": user.email})

    token = create_unsubscribe_token(user.email)
    resp = await client.get(f"{UNSUBSCRIBE_PATH}?token={token}")

    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "unsubscribed" in resp.text.lower()

    db_session.expire_all()
    assert crud_email_suppression.is_suppressed(db_session, email=user.email) is True

    row = (
        db_session.query(NewsletterSubscriber)
        .filter(NewsletterSubscriber.email == user.email)
        .one()
    )
    assert row.is_active is False

    current = crud_user_consent.current_map(db_session, user_id=user.id)
    assert current["marketing_email"] is False
    assert current["newsletter"] is False


async def test_unsubscribe_works_for_address_with_no_account(client, db_session):
    """Company contacts and anonymous subscribers have no user row — the
    suppression list is keyed by address precisely so they can still opt out."""
    token = create_unsubscribe_token("stranger@example.com")
    resp = await client.get(f"{UNSUBSCRIBE_PATH}?token={token}")

    assert resp.status_code == 200
    assert crud_email_suppression.is_suppressed(db_session, email="stranger@example.com") is True


async def test_unsubscribe_is_idempotent(client, db_session):
    """Mail clients pre-fetch links and people click twice; neither may 500."""
    token = create_unsubscribe_token("twice@example.com")
    first = await client.get(f"{UNSUBSCRIBE_PATH}?token={token}")
    second = await client.get(f"{UNSUBSCRIBE_PATH}?token={token}")

    assert first.status_code == 200
    assert second.status_code == 200
    assert (
        db_session.query(EmailSuppression)
        .filter(EmailSuppression.email == "twice@example.com")
        .count()
        == 1
    )


async def test_unsubscribe_rejects_a_token_of_the_wrong_type(client, db_session):
    """An access token must not double as an unsubscribe token — the ``type``
    claim is what stops one credential being replayed as another."""
    access_token, _, _ = create_access_token(subject="1")
    resp = await client.get(f"{UNSUBSCRIBE_PATH}?token={access_token}")

    assert resp.status_code == 400
    assert db_session.query(EmailSuppression).count() == 0


async def test_unsubscribe_rejects_garbage_token(client, db_session):
    resp = await client.get(f"{UNSUBSCRIBE_PATH}?token=not-a-jwt")
    assert resp.status_code == 400
    assert db_session.query(EmailSuppression).count() == 0


async def test_one_click_post_unsubscribes(client, db_session):
    """RFC 8058: providers POST here with no body and no auth."""
    token = create_unsubscribe_token("oneclick@example.com")
    resp = await client.post(f"{UNSUBSCRIBE_PATH}?token={token}")

    assert resp.status_code == 200
    assert resp.json() == {"status": "unsubscribed"}
    assert crud_email_suppression.is_suppressed(db_session, email="oneclick@example.com") is True


async def test_one_click_post_returns_200_on_a_bad_token(client, db_session):
    """Reporting failure to the provider shows the recipient "unsubscribe didn't
    work", which sends them to the spam button instead. Nothing is suppressed."""
    resp = await client.post(f"{UNSUBSCRIBE_PATH}?token=garbage")

    assert resp.status_code == 200
    assert resp.json() == {"status": "unsubscribed"}
    assert db_session.query(EmailSuppression).count() == 0


async def test_resubscribe_after_unsubscribe_clears_nothing_by_itself(client, db_session):
    """Subscribing again reactivates the newsletter row but must NOT silently
    lift the suppression — coming back requires an explicit re-opt-in path, so
    a stray form submit can never resurrect a stopped address."""
    email = "returning@example.com"
    await client.post(SUBSCRIBE_PATH, json={"email": email})
    await client.get(f"{UNSUBSCRIBE_PATH}?token={create_unsubscribe_token(email)}")

    await client.post(SUBSCRIBE_PATH, json={"email": email})

    db_session.expire_all()
    assert crud_email_suppression.is_suppressed(db_session, email=email) is True
