"""Integration tests: the order lifecycle schedules the right email event.

The real ``send_order_event_in_background`` opens its own DB session (which can't
see the test's savepoint-isolated rows), so we stub it with a capturing fake and
assert the route scheduled the correct ``(order_id, event)`` via BackgroundTasks.
The email *composition* is covered in ``tests/unit/test_order_email.py``.
"""

from __future__ import annotations

import pytest

from app.core.security import create_access_token
from app.models.user import UserRole
from app.services import order_email

pytestmark = pytest.mark.integration


@pytest.fixture
def scheduled_emails(monkeypatch) -> list[tuple[int, str]]:
    """Capture (order_id, event) the routes schedule, without opening a session."""
    calls: list[tuple[int, str]] = []
    monkeypatch.setattr(
        order_email,
        "send_order_event_in_background",
        lambda order_id, event: calls.append((order_id, event)),
    )
    return calls


def _headers(user) -> dict:
    token, _, _ = create_access_token(subject=str(user.id))
    return {"Authorization": f"Bearer {token}"}


def _order_body(product_id: int, quantity: int = 1) -> dict:
    return {
        "items": [{"product_id": product_id, "quantity": quantity}],
        "currency": "USD",
        "shipping_address": "1 Test St",
        "shipping_city": "Town",
        "shipping_postal_code": "00-001",
        "shipping_country": "USA",
    }


async def test_create_schedules_confirmation(
    client, user_factory, product_factory, scheduled_emails
):
    user = user_factory()
    product = product_factory(stock=5, price_usd=20.0)
    resp = await client.post(
        "/api/v1/orders/", json=_order_body(product.id), headers=_headers(user)
    )
    assert resp.status_code == 201
    assert (resp.json()["id"], "confirmation") in scheduled_emails


async def test_admin_status_change_schedules_event(
    client, user_factory, product_factory, scheduled_emails
):
    customer = user_factory(email="evt-cust@example.com")
    admin = user_factory(email="evt-admin@example.com", role=UserRole.ADMIN)
    product = product_factory(stock=5, price_usd=20.0)
    created = await client.post(
        "/api/v1/orders/", json=_order_body(product.id), headers=_headers(customer)
    )
    order_id = created.json()["id"]
    scheduled_emails.clear()  # drop the confirmation scheduled by create

    resp = await client.patch(
        f"/api/v1/orders/{order_id}", json={"status": "paid"}, headers=_headers(admin)
    )
    assert resp.status_code == 200
    assert (order_id, "paid") in scheduled_emails


async def test_admin_notes_only_schedules_nothing(
    client, user_factory, product_factory, scheduled_emails
):
    customer = user_factory(email="notes-cust@example.com")
    admin = user_factory(email="notes-admin@example.com", role=UserRole.ADMIN)
    product = product_factory(stock=5, price_usd=20.0)
    created = await client.post(
        "/api/v1/orders/", json=_order_body(product.id), headers=_headers(customer)
    )
    order_id = created.json()["id"]
    scheduled_emails.clear()

    # Status unchanged (notes-only PATCH) → no email.
    resp = await client.patch(
        f"/api/v1/orders/{order_id}", json={"notes": "leave at door"}, headers=_headers(admin)
    )
    assert resp.status_code == 200
    assert scheduled_emails == []


async def test_cancel_schedules_cancelled(client, user_factory, product_factory, scheduled_emails):
    user = user_factory(email="cancel-cust@example.com")
    product = product_factory(stock=5, price_usd=20.0)
    created = await client.post(
        "/api/v1/orders/", json=_order_body(product.id), headers=_headers(user)
    )
    order_id = created.json()["id"]
    scheduled_emails.clear()

    resp = await client.post(f"/api/v1/orders/{order_id}/cancel", headers=_headers(user))
    assert resp.status_code == 200
    assert (order_id, "cancelled") in scheduled_emails
