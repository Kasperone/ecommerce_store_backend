"""Integration tests for the public cookie-consent API (/api/v1/cookie-consent).

GDPR/ePrivacy proof-of-choice storage. Both endpoints are PUBLIC and never 401 a
guest. POST is append-only; ``necessary`` is always coerced true server-side; a
valid bearer attaches ``user_id``; ip/ua + the client GPC flag are captured. GET
returns the latest choice (200 null when none), resolving by ``anon_id`` and/or
the authed user.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.models.cookie_consent import CookieConsent

pytestmark = [pytest.mark.integration, pytest.mark.rate_limit]


def _headers(user) -> dict:
    token, _, _ = create_access_token(subject=str(user.id))
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# POST — guests
# ---------------------------------------------------------------------------


async def test_guest_can_post_no_auth(client, reset_limiter):
    """No token → 200; a guest can record consent."""
    resp = await client.post(
        "/api/v1/cookie-consent",
        json={
            "anon_id": "anon-guest-1",
            "categories": {"analytics": True, "marketing": False},
            "gpc": False,
            "policy_version": "v1",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["categories"] == {"necessary": True, "analytics": True, "marketing": False}
    assert body["gpc"] is False
    assert body["policy_version"] == "v1"
    assert "created_at" in body


async def test_necessary_always_coerced_true(client, reset_limiter):
    """Client sends necessary=false → server stores it as true regardless."""
    resp = await client.post(
        "/api/v1/cookie-consent",
        json={
            "anon_id": "anon-coerce",
            "categories": {"necessary": False, "analytics": False, "marketing": False},
        },
    )
    assert resp.status_code == 200
    assert resp.json()["categories"]["necessary"] is True


async def test_gpc_flag_stored(client, reset_limiter):
    """The GPC signal the client sends is honored (stored)."""
    resp = await client.post(
        "/api/v1/cookie-consent",
        json={"anon_id": "anon-gpc", "categories": {"analytics": True}, "gpc": True},
    )
    assert resp.status_code == 200
    assert resp.json()["gpc"] is True


async def test_post_is_append_only(client, db_session: Session, reset_limiter):
    """Two POSTs for the same anon_id create TWO rows (never updated)."""
    for analytics in (True, False):
        resp = await client.post(
            "/api/v1/cookie-consent",
            json={"anon_id": "anon-append", "categories": {"analytics": analytics}},
        )
        assert resp.status_code == 200

    rows = db_session.query(CookieConsent).filter(CookieConsent.anon_id == "anon-append").all()
    assert len(rows) == 2


async def test_ip_and_ua_captured(client, db_session: Session, reset_limiter):
    resp = await client.post(
        "/api/v1/cookie-consent",
        json={"anon_id": "anon-ipua", "categories": {"analytics": True}},
        headers={"User-Agent": "PyTest-UA/1.0"},
    )
    assert resp.status_code == 200
    row = db_session.query(CookieConsent).filter(CookieConsent.anon_id == "anon-ipua").first()
    assert row is not None
    assert row.user_agent == "PyTest-UA/1.0"
    assert row.ip is not None  # "testclient" under ASGI


# ---------------------------------------------------------------------------
# POST — authed
# ---------------------------------------------------------------------------


async def test_authed_post_attaches_user_id(
    client, db_session: Session, user_factory, reset_limiter
):
    user = user_factory(email="cookie-authed@example.com")
    resp = await client.post(
        "/api/v1/cookie-consent",
        json={"anon_id": "anon-authed", "categories": {"analytics": True}},
        headers=_headers(user),
    )
    assert resp.status_code == 200
    row = db_session.query(CookieConsent).filter(CookieConsent.anon_id == "anon-authed").first()
    assert row is not None
    assert row.user_id == user.id


async def test_invalid_bearer_treated_as_guest(client, db_session: Session, reset_limiter):
    """A malformed/invalid token must NOT 401 — it's a guest."""
    resp = await client.post(
        "/api/v1/cookie-consent",
        json={"anon_id": "anon-badtok", "categories": {"analytics": True}},
        headers={"Authorization": "Bearer not-a-real-token"},
    )
    assert resp.status_code == 200
    row = db_session.query(CookieConsent).filter(CookieConsent.anon_id == "anon-badtok").first()
    assert row is not None
    assert row.user_id is None


# ---------------------------------------------------------------------------
# GET — latest
# ---------------------------------------------------------------------------


async def test_get_returns_200_null_when_none(client):
    resp = await client.get("/api/v1/cookie-consent", params={"anon_id": "nobody-here"})
    assert resp.status_code == 200
    assert resp.json() is None


async def test_get_no_anon_id_returns_null(client):
    """No anon_id and no auth → nothing to resolve → 200 null."""
    resp = await client.get("/api/v1/cookie-consent")
    assert resp.status_code == 200
    assert resp.json() is None


async def test_get_returns_latest_for_anon_id(client, reset_limiter):
    for analytics in (False, True):
        await client.post(
            "/api/v1/cookie-consent",
            json={"anon_id": "anon-latest", "categories": {"analytics": analytics}},
        )
    resp = await client.get("/api/v1/cookie-consent", params={"anon_id": "anon-latest"})
    assert resp.status_code == 200
    assert resp.json()["categories"]["analytics"] is True  # latest wins


async def test_get_resolves_authed_user(client, user_factory, reset_limiter):
    user = user_factory(email="cookie-get-authed@example.com")
    await client.post(
        "/api/v1/cookie-consent",
        json={"categories": {"marketing": True}},
        headers=_headers(user),
    )
    # No anon_id on the GET — resolution is purely via the bearer.
    resp = await client.get("/api/v1/cookie-consent", headers=_headers(user))
    assert resp.status_code == 200
    assert resp.json()["categories"]["marketing"] is True
