"""Integration tests for inactive-product visibility on ``GET /api/v1/products/``.

The product list is a public endpoint shared by the storefront and the admin
catalog. Inactive (unpublished / discontinued) products must never be
enumerable by guests — even if they explicitly ask for ``is_active=false`` —
while an admin may request inactive-only or the unfiltered "all" view (by
omitting the param). Guards the auth-aware ``is_active`` handling in
``list_products``.
"""

from __future__ import annotations

import pytest

from app.core.security import create_access_token
from app.models.user import UserRole

pytestmark = pytest.mark.integration

LIST_URL = "/api/v1/products/"


def _headers(user) -> dict:
    token, _, _ = create_access_token(subject=str(user.id))
    return {"Authorization": f"Bearer {token}"}


def _names(payload: dict) -> set[str]:
    return {item["name"] for item in payload["items"]}


@pytest.fixture
def seeded(product_factory):
    """One active + one inactive product, returned as (active, inactive)."""
    active = product_factory(name="VisibleActive", slug="vis-active", is_active=True)
    inactive = product_factory(name="HiddenInactive", slug="vis-inactive", is_active=False)
    return active, inactive


async def test_guest_default_sees_only_active(client, seeded):
    """A guest omitting is_active gets active products only (no inactive)."""
    resp = await client.get(LIST_URL, params={"page_size": 100})
    assert resp.status_code == 200
    names = _names(resp.json())
    assert "VisibleActive" in names
    assert "HiddenInactive" not in names


async def test_guest_cannot_request_inactive(client, seeded):
    """is_active=false from a guest is ignored — never leaks inactive products."""
    resp = await client.get(LIST_URL, params={"is_active": "false", "page_size": 100})
    assert resp.status_code == 200
    names = _names(resp.json())
    assert "HiddenInactive" not in names
    assert "VisibleActive" in names


async def test_customer_cannot_request_inactive(client, user_factory, seeded):
    """A non-admin authenticated user is treated like a guest for visibility."""
    customer = user_factory(email="vis-cust@example.com")
    resp = await client.get(
        LIST_URL, params={"is_active": "false", "page_size": 100}, headers=_headers(customer)
    )
    assert resp.status_code == 200
    assert "HiddenInactive" not in _names(resp.json())


async def test_admin_default_sees_all(client, user_factory, seeded):
    """An admin omitting is_active gets the unfiltered view (active + inactive)."""
    admin = user_factory(email="vis-admin@example.com", role=UserRole.ADMIN)
    resp = await client.get(LIST_URL, params={"page_size": 100}, headers=_headers(admin))
    assert resp.status_code == 200
    names = _names(resp.json())
    assert {"VisibleActive", "HiddenInactive"} <= names


async def test_admin_can_filter_inactive_only(client, user_factory, seeded):
    """An admin may request inactive-only via is_active=false."""
    admin = user_factory(email="vis-admin2@example.com", role=UserRole.ADMIN)
    resp = await client.get(
        LIST_URL, params={"is_active": "false", "page_size": 100}, headers=_headers(admin)
    )
    assert resp.status_code == 200
    names = _names(resp.json())
    assert "HiddenInactive" in names
    assert "VisibleActive" not in names


async def test_admin_can_filter_active_only(client, user_factory, seeded):
    """An admin may still request active-only via is_active=true."""
    admin = user_factory(email="vis-admin3@example.com", role=UserRole.ADMIN)
    resp = await client.get(
        LIST_URL, params={"is_active": "true", "page_size": 100}, headers=_headers(admin)
    )
    assert resp.status_code == 200
    names = _names(resp.json())
    assert "VisibleActive" in names
    assert "HiddenInactive" not in names
