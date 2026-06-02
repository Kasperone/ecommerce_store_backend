"""Integration tests for marketing consent on `/api/v1/me/preferences`.

Consent is append-only (D-B-5): each PATCH writing a ``consents`` partial INSERTs
a new ``user_consents`` row, and GET returns the latest-per-type snapshot. Strictly
owner-scoped (user derived from the bearer token).
"""

from __future__ import annotations

import pytest

from app.core.security import create_access_token
from app.models.user_consent import UserConsent

pytestmark = pytest.mark.integration


def _headers(user) -> dict:
    token, _, _ = create_access_token(subject=str(user.id))
    return {"Authorization": f"Bearer {token}"}


async def test_patch_records_consent_and_get_reflects_it(client, user_factory):
    user = user_factory(email="consent-set@example.com")
    resp = await client.patch(
        "/api/v1/me/preferences",
        json={"consents": {"marketing_email": True}},
        headers=_headers(user),
    )
    assert resp.status_code == 200
    assert resp.json()["consents"] == {"marketing_email": True}

    get_resp = await client.get("/api/v1/me/preferences", headers=_headers(user))
    assert get_resp.json()["consents"] == {"marketing_email": True}


async def test_consent_is_append_only(client, db_session, user_factory):
    """Two PATCHes for the same type create TWO rows; GET returns the latest."""
    user = user_factory(email="consent-append@example.com")

    await client.patch(
        "/api/v1/me/preferences",
        json={"consents": {"marketing_email": True}},
        headers=_headers(user),
    )
    await client.patch(
        "/api/v1/me/preferences",
        json={"consents": {"marketing_email": False}},
        headers=_headers(user),
    )

    rows = (
        db_session.query(UserConsent)
        .filter(UserConsent.user_id == user.id, UserConsent.consent_type == "marketing_email")
        .all()
    )
    assert len(rows) == 2  # never updated in place

    get_resp = await client.get("/api/v1/me/preferences", headers=_headers(user))
    assert get_resp.json()["consents"] == {"marketing_email": False}  # latest wins


async def test_consent_captures_source_and_user_agent(client, db_session, user_factory):
    user = user_factory(email="consent-meta@example.com")
    await client.patch(
        "/api/v1/me/preferences",
        json={"consents": {"newsletter": True}},
        headers={**_headers(user), "User-Agent": "pytest-agent/1.0"},
    )
    row = db_session.query(UserConsent).filter(UserConsent.user_id == user.id).one()
    assert row.source == "preferences"
    assert row.user_agent == "pytest-agent/1.0"


async def test_consent_and_currency_in_one_patch(client, db_session, user_factory):
    """A single PATCH can change currency AND record a consent."""
    user = user_factory(email="consent-combo@example.com")
    resp = await client.patch(
        "/api/v1/me/preferences",
        json={"preferred_currency": "PLN", "consents": {"marketing_email": True}},
        headers=_headers(user),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["preferred_currency"] == "PLN"
    assert body["consents"] == {"marketing_email": True}

    db_session.refresh(user)
    assert user.preferred_currency == "PLN"


async def test_multiple_consent_types_independent(client, user_factory):
    user = user_factory(email="consent-multi@example.com")
    await client.patch(
        "/api/v1/me/preferences",
        json={"consents": {"marketing_email": True, "newsletter": False}},
        headers=_headers(user),
    )
    get_resp = await client.get("/api/v1/me/preferences", headers=_headers(user))
    assert get_resp.json()["consents"] == {"marketing_email": True, "newsletter": False}


async def test_consent_requires_auth(client):
    resp = await client.patch(
        "/api/v1/me/preferences",
        json={"consents": {"marketing_email": True}},
    )
    assert resp.status_code == 401


async def test_consent_owner_scoped(client, user_factory):
    """User A's consent never appears in user B's snapshot."""
    user_a = user_factory(email="consent-owner-a@example.com")
    user_b = user_factory(email="consent-owner-b@example.com")
    await client.patch(
        "/api/v1/me/preferences",
        json={"consents": {"marketing_email": True}},
        headers=_headers(user_a),
    )
    resp_b = await client.get("/api/v1/me/preferences", headers=_headers(user_b))
    assert resp_b.json()["consents"] == {}
