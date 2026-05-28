"""Integration tests for the ``User.locale`` email-language preference.

Captured at registration (optional, default "en") and settable via ``PUT /auth/me``.
The order-email service reads it at send time.
"""

from __future__ import annotations

import pytest

from app.core.security import create_access_token
from app.crud import user as crud_user

pytestmark = pytest.mark.integration


def _headers(user) -> dict:
    token, _, _ = create_access_token(subject=str(user.id))
    return {"Authorization": f"Bearer {token}"}


async def test_default_locale_is_en(client, user_factory):
    user = user_factory(email="loc-default@example.com")
    resp = await client.get("/api/v1/auth/me", headers=_headers(user))
    assert resp.status_code == 200
    assert resp.json()["locale"] == "en"


async def test_update_locale_to_pl(client, user_factory):
    user = user_factory(email="loc-update@example.com")
    resp = await client.put("/api/v1/auth/me", json={"locale": "pl"}, headers=_headers(user))
    assert resp.status_code == 200
    assert resp.json()["locale"] == "pl"


async def test_invalid_locale_rejected(client, user_factory):
    user = user_factory(email="loc-bad@example.com")
    resp = await client.put("/api/v1/auth/me", json={"locale": "fr"}, headers=_headers(user))
    assert resp.status_code == 422


async def test_register_captures_locale(client, db_session, captured_emails, reset_limiter):
    payload = {
        "email": "loc-reg@example.com",
        "password": "Str0ngPass!",
        "first_name": "Reg",
        "last_name": "User",
        "locale": "pl",
    }
    resp = await client.post("/api/v1/auth/register", json=payload)
    assert resp.status_code == 200

    user = crud_user.get_by_email(db_session, email="loc-reg@example.com")
    assert user is not None
    assert user.locale == "pl"
