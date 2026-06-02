"""Integration tests for the address-book endpoints (`/api/v1/addresses`).

Owner-scoped: the user is derived from the bearer token and every lookup is
gated on ``(id, user_id)``, so a cross-user or unknown id returns **404** (never
403). Default-flag mutual-exclusion and first-address-auto-default are enforced
transactionally in the CRUD layer; these tests exercise them through the HTTP
surface.
"""

from __future__ import annotations

import pytest

from app.core.security import create_access_token

pytestmark = pytest.mark.integration


def _headers(user) -> dict:
    token, _, _ = create_access_token(subject=str(user.id))
    return {"Authorization": f"Bearer {token}"}


async def _by_id(client, user) -> dict:
    """Fetch the caller's addresses and key them by id."""
    resp = await client.get("/api/v1/addresses", headers=_headers(user))
    return {a["id"]: a for a in resp.json()}


def _payload(**overrides) -> dict:
    base = {
        "recipient_name": "Jane Doe",
        "street": "1 Main St",
        "city": "Warsaw",
        "postal_code": "00-001",
        "country": "PL",
    }
    base.update(overrides)
    return base


# --- auth ------------------------------------------------------------------


async def test_list_requires_auth(client):
    resp = await client.get("/api/v1/addresses")
    assert resp.status_code == 401


async def test_create_requires_auth(client):
    resp = await client.post("/api/v1/addresses", json=_payload())
    assert resp.status_code == 401


# --- list ------------------------------------------------------------------


async def test_list_empty(client, user_factory):
    user = user_factory(email="addr-empty@example.com")
    resp = await client.get("/api/v1/addresses", headers=_headers(user))
    assert resp.status_code == 200
    assert resp.json() == []


async def test_list_default_shipping_first_then_newest(client, user_factory):
    """Ordering: the default-shipping row first, then most-recently created."""
    user = user_factory(email="addr-order@example.com")
    # First address auto-becomes both defaults.
    await client.post("/api/v1/addresses", json=_payload(city="First"), headers=_headers(user))
    # Second, non-default.
    await client.post("/api/v1/addresses", json=_payload(city="Second"), headers=_headers(user))
    # Third, non-default (newest).
    await client.post("/api/v1/addresses", json=_payload(city="Third"), headers=_headers(user))

    resp = await client.get("/api/v1/addresses", headers=_headers(user))
    cities = [a["city"] for a in resp.json()]
    # Default (First) leads; the rest are newest-first.
    assert cities == ["First", "Third", "Second"]


# --- create ----------------------------------------------------------------


async def test_create_returns_201_and_body(client, user_factory):
    user = user_factory(email="addr-create@example.com")
    resp = await client.post(
        "/api/v1/addresses",
        json=_payload(label="Home", state="Mazovia", phone="+48123"),
        headers=_headers(user),
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["label"] == "Home"
    assert body["recipient_name"] == "Jane Doe"
    assert body["user_id"] == user.id
    assert "id" in body and "created_at" in body


async def test_first_address_auto_becomes_both_defaults(client, user_factory):
    user = user_factory(email="addr-first@example.com")
    resp = await client.post(
        "/api/v1/addresses",
        json=_payload(is_default_shipping=False, is_default_billing=False),
        headers=_headers(user),
    )
    body = resp.json()
    # Even though the caller asked for non-default, the first address is forced.
    assert body["is_default_shipping"] is True
    assert body["is_default_billing"] is True


async def test_second_address_respects_requested_flags(client, user_factory):
    user = user_factory(email="addr-second@example.com")
    await client.post("/api/v1/addresses", json=_payload(), headers=_headers(user))
    resp = await client.post(
        "/api/v1/addresses",
        json=_payload(city="Second", is_default_shipping=False, is_default_billing=False),
        headers=_headers(user),
    )
    body = resp.json()
    assert body["is_default_shipping"] is False
    assert body["is_default_billing"] is False


async def test_create_missing_required_field_422(client, user_factory):
    user = user_factory(email="addr-missing@example.com")
    bad = _payload()
    del bad["city"]
    resp = await client.post("/api/v1/addresses", json=bad, headers=_headers(user))
    assert resp.status_code == 422


# --- default mutual-exclusion ----------------------------------------------


async def test_creating_new_default_shipping_clears_prior(client, user_factory):
    user = user_factory(email="addr-mx-create@example.com")
    first = (
        await client.post("/api/v1/addresses", json=_payload(city="A"), headers=_headers(user))
    ).json()
    # New default-shipping address.
    second = (
        await client.post(
            "/api/v1/addresses",
            json=_payload(city="B", is_default_shipping=True),
            headers=_headers(user),
        )
    ).json()

    addrs = await _by_id(client, user)
    assert addrs[second["id"]]["is_default_shipping"] is True
    assert addrs[first["id"]]["is_default_shipping"] is False
    # Billing on the first is untouched (only shipping was promoted).
    assert addrs[first["id"]]["is_default_billing"] is True


async def test_update_promoting_default_clears_prior(client, user_factory):
    user = user_factory(email="addr-mx-update@example.com")
    first = (
        await client.post("/api/v1/addresses", json=_payload(city="A"), headers=_headers(user))
    ).json()
    second = (
        await client.post("/api/v1/addresses", json=_payload(city="B"), headers=_headers(user))
    ).json()

    # Promote the second to default-billing via PUT.
    resp = await client.put(
        f"/api/v1/addresses/{second['id']}",
        json={"is_default_billing": True},
        headers=_headers(user),
    )
    assert resp.status_code == 200
    assert resp.json()["is_default_billing"] is True

    addrs = await _by_id(client, user)
    assert addrs[second["id"]]["is_default_billing"] is True
    assert addrs[first["id"]]["is_default_billing"] is False


# --- update ----------------------------------------------------------------


async def test_update_partial_changes_only_sent_fields(client, user_factory):
    user = user_factory(email="addr-update@example.com")
    created = (
        await client.post("/api/v1/addresses", json=_payload(label="Home"), headers=_headers(user))
    ).json()
    resp = await client.put(
        f"/api/v1/addresses/{created['id']}",
        json={"label": "Office"},
        headers=_headers(user),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["label"] == "Office"
    # Untouched fields survive.
    assert body["city"] == "Warsaw"


async def test_update_unknown_id_404(client, user_factory):
    user = user_factory(email="addr-upd-404@example.com")
    resp = await client.put("/api/v1/addresses/999999", json={"label": "X"}, headers=_headers(user))
    assert resp.status_code == 404


# --- delete ----------------------------------------------------------------


async def test_delete_returns_204(client, user_factory):
    user = user_factory(email="addr-del@example.com")
    created = (
        await client.post("/api/v1/addresses", json=_payload(), headers=_headers(user))
    ).json()
    resp = await client.delete(f"/api/v1/addresses/{created['id']}", headers=_headers(user))
    assert resp.status_code == 204
    # Gone from the list.
    remaining = (await client.get("/api/v1/addresses", headers=_headers(user))).json()
    assert remaining == []


async def test_delete_unknown_id_404(client, user_factory):
    user = user_factory(email="addr-del-404@example.com")
    resp = await client.delete("/api/v1/addresses/999999", headers=_headers(user))
    assert resp.status_code == 404


# --- owner gating ----------------------------------------------------------


async def test_cannot_read_other_users_address_via_list(client, user_factory):
    """The list is owner-scoped: B never sees A's rows."""
    user_a = user_factory(email="addr-own-a@example.com")
    user_b = user_factory(email="addr-own-b@example.com")
    await client.post("/api/v1/addresses", json=_payload(), headers=_headers(user_a))

    resp_b = await client.get("/api/v1/addresses", headers=_headers(user_b))
    assert resp_b.json() == []


async def test_cannot_update_other_users_address_404(client, user_factory):
    user_a = user_factory(email="addr-x-upd-a@example.com")
    user_b = user_factory(email="addr-x-upd-b@example.com")
    created = (
        await client.post("/api/v1/addresses", json=_payload(), headers=_headers(user_a))
    ).json()

    # B tries to update A's address → 404 (not 403 — doesn't reveal existence).
    resp = await client.put(
        f"/api/v1/addresses/{created['id']}",
        json={"label": "Hijack"},
        headers=_headers(user_b),
    )
    assert resp.status_code == 404


async def test_cannot_delete_other_users_address_404(client, user_factory):
    user_a = user_factory(email="addr-x-del-a@example.com")
    user_b = user_factory(email="addr-x-del-b@example.com")
    created = (
        await client.post("/api/v1/addresses", json=_payload(), headers=_headers(user_a))
    ).json()

    resp = await client.delete(f"/api/v1/addresses/{created['id']}", headers=_headers(user_b))
    assert resp.status_code == 404
    # A's address still exists.
    a_list = (await client.get("/api/v1/addresses", headers=_headers(user_a))).json()
    assert len(a_list) == 1
