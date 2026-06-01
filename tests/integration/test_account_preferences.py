"""Integration tests for the self-service preferences endpoints (`/api/v1/me/*`).

`GET /me/preferences` returns the caller's own currency + locale snapshot;
`PATCH /me/preferences` partially updates it. Strictly owner-scoped (user derived
from the bearer token, never a body id).
"""

from __future__ import annotations

import pytest

from app.core.security import create_access_token

pytestmark = pytest.mark.integration


def _headers(user) -> dict:
    token, _, _ = create_access_token(subject=str(user.id))
    return {"Authorization": f"Bearer {token}"}


# --- GET -------------------------------------------------------------------


async def test_get_preferences_defaults(client, user_factory):
    """A fresh user has no preferred_currency (NULL) and locale 'en'."""
    user = user_factory(email="pref-get@example.com")
    resp = await client.get("/api/v1/me/preferences", headers=_headers(user))
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"preferred_currency": None, "locale": "en"}


async def test_get_preferences_requires_auth(client):
    resp = await client.get("/api/v1/me/preferences")
    assert resp.status_code == 401


# --- PATCH happy paths -----------------------------------------------------


async def test_patch_sets_currency(client, db_session, user_factory):
    user = user_factory(email="pref-cur@example.com")
    resp = await client.patch(
        "/api/v1/me/preferences",
        json={"preferred_currency": "PLN"},
        headers=_headers(user),
    )
    assert resp.status_code == 200
    assert resp.json() == {"preferred_currency": "PLN", "locale": "en"}

    db_session.refresh(user)
    assert user.preferred_currency == "PLN"


async def test_patch_can_update_locale(client, user_factory):
    user = user_factory(email="pref-loc@example.com")
    resp = await client.patch(
        "/api/v1/me/preferences",
        json={"locale": "pl"},
        headers=_headers(user),
    )
    assert resp.status_code == 200
    assert resp.json() == {"preferred_currency": None, "locale": "pl"}


async def test_patch_partial_leaves_other_field_unchanged(client, user_factory):
    """Setting only the locale must not wipe a previously-set currency."""
    user = user_factory(email="pref-partial@example.com")
    # Seed a currency first.
    await client.patch(
        "/api/v1/me/preferences",
        json={"preferred_currency": "EUR"},
        headers=_headers(user),
    )
    # Now update only the locale.
    resp = await client.patch(
        "/api/v1/me/preferences",
        json={"locale": "pl"},
        headers=_headers(user),
    )
    assert resp.status_code == 200
    assert resp.json() == {"preferred_currency": "EUR", "locale": "pl"}


async def test_patch_null_clears_currency(client, db_session, user_factory):
    """Explicit null clears the preference (falls back to locale default)."""
    user = user_factory(email="pref-clear@example.com")
    await client.patch(
        "/api/v1/me/preferences",
        json={"preferred_currency": "USD"},
        headers=_headers(user),
    )
    resp = await client.patch(
        "/api/v1/me/preferences",
        json={"preferred_currency": None},
        headers=_headers(user),
    )
    assert resp.status_code == 200
    assert resp.json()["preferred_currency"] is None

    db_session.refresh(user)
    assert user.preferred_currency is None


async def test_patch_empty_body_is_noop(client, user_factory):
    """An empty PATCH body changes nothing and returns the current snapshot."""
    user = user_factory(email="pref-empty@example.com")
    await client.patch(
        "/api/v1/me/preferences",
        json={"preferred_currency": "EUR"},
        headers=_headers(user),
    )
    resp = await client.patch(
        "/api/v1/me/preferences",
        json={},
        headers=_headers(user),
    )
    assert resp.status_code == 200
    assert resp.json() == {"preferred_currency": "EUR", "locale": "en"}


# --- PATCH validation ------------------------------------------------------


async def test_patch_bad_currency_rejected(client, user_factory):
    user = user_factory(email="pref-badcur@example.com")
    resp = await client.patch(
        "/api/v1/me/preferences",
        json={"preferred_currency": "GBP"},
        headers=_headers(user),
    )
    assert resp.status_code == 422


async def test_patch_bad_locale_rejected(client, user_factory):
    user = user_factory(email="pref-badloc@example.com")
    resp = await client.patch(
        "/api/v1/me/preferences",
        json={"locale": "fr"},
        headers=_headers(user),
    )
    assert resp.status_code == 422


async def test_patch_requires_auth(client):
    resp = await client.patch(
        "/api/v1/me/preferences",
        json={"preferred_currency": "USD"},
    )
    assert resp.status_code == 401


# --- Owner scoping ---------------------------------------------------------


async def test_preferences_are_owner_scoped(client, db_session, user_factory):
    """Each caller only ever reads/writes their own row — there is no body id, so
    user A's PATCH can never affect user B."""
    user_a = user_factory(email="pref-owner-a@example.com")
    user_b = user_factory(email="pref-owner-b@example.com")

    await client.patch(
        "/api/v1/me/preferences",
        json={"preferred_currency": "PLN"},
        headers=_headers(user_a),
    )

    # B's snapshot is untouched.
    resp_b = await client.get("/api/v1/me/preferences", headers=_headers(user_b))
    assert resp_b.json()["preferred_currency"] is None

    db_session.refresh(user_b)
    assert user_b.preferred_currency is None
    db_session.refresh(user_a)
    assert user_a.preferred_currency == "PLN"
