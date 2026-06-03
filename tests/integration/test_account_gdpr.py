"""Integration tests for the GDPR endpoints on `/api/v1/me/*`.

`POST /me/data-export` returns the caller's profile + addresses + orders + consent
trail (Art. 15/20). `POST /me/account-deletion-request` soft-deletes: it stamps the
request and anonymizes PII immediately (name/phone/email + shipping/company),
deletes addresses, revokes refresh tokens, while keeping order/invoice rows
(Art. 17 / D-B-4). Strictly owner-scoped.
"""

from __future__ import annotations

import pytest

from app.core.security import create_access_token
from app.models.order import Order, OrderItem
from app.models.user_consent import UserConsent

pytestmark = pytest.mark.integration


def _headers(user) -> dict:
    token, _, _ = create_access_token(subject=str(user.id))
    return {"Authorization": f"Bearer {token}"}


def _make_order(db, user, *, product_id: int, order_number: str = "ORD-TEST-0001") -> Order:
    """Insert a minimal order + one item for ``user``."""
    order = Order(
        user_id=user.id,
        order_number=order_number,
        subtotal=100.0,
        total=100.0,
        currency="USD",
        shipping_address="1 Main St",
        shipping_city="Warsaw",
        shipping_postal_code="00-001",
        shipping_country="PL",
    )
    db.add(order)
    db.flush()
    db.add(
        OrderItem(
            order_id=order.id,
            product_id=product_id,
            product_name="Widget",
            quantity=2,
            unit_price=50.0,
            total_price=100.0,
        )
    )
    db.commit()
    db.refresh(order)
    return order


def _make_address(client, user, **overrides) -> dict:
    payload = {
        "recipient_name": "Jane Doe",
        "street": "1 Main St",
        "city": "Warsaw",
        "postal_code": "00-001",
        "country": "PL",
    }
    payload.update(overrides)
    return payload


# --- data export -----------------------------------------------------------


async def test_data_export_shape_and_contents(client, db_session, user_factory, product_factory):
    product = product_factory()
    user = user_factory(
        email="export-shape@example.com",
        first_name="Anna",
        last_name="Nowak",
        phone="+48111222333",
    )
    # An address (via the public router so the CRUD defaults apply).
    await client.post(
        "/api/v1/addresses",
        json=_make_address(client, user),
        headers=_headers(user),
    )
    # A consent.
    await client.patch(
        "/api/v1/me/preferences",
        json={"consents": {"marketing_email": True}},
        headers=_headers(user),
    )
    # An order.
    _make_order(db_session, user, product_id=product.id, order_number="ORD-EXPORT-1")

    resp = await client.post("/api/v1/me/data-export", headers=_headers(user))
    assert resp.status_code == 200
    body = resp.json()

    assert set(body.keys()) == {"user", "addresses", "orders", "consents"}
    assert body["user"]["email"] == "export-shape@example.com"
    assert body["user"]["first_name"] == "Anna"
    # Secrets never leak.
    assert "hashed_password" not in body["user"]
    assert "password_reset_token_hash" not in body["user"]

    assert len(body["addresses"]) == 1
    assert body["addresses"][0]["city"] == "Warsaw"

    assert len(body["orders"]) == 1
    assert body["orders"][0]["order_number"] == "ORD-EXPORT-1"
    assert len(body["orders"][0]["items"]) == 1
    assert body["orders"][0]["items"][0]["product_name"] == "Widget"

    assert len(body["consents"]) == 1
    assert body["consents"][0]["consent_type"] == "marketing_email"


async def test_data_export_owner_scoped(client, db_session, user_factory, product_factory):
    """User A's export only ever contains user A's data."""
    product = product_factory()
    user_a = user_factory(email="export-a@example.com")
    user_b = user_factory(email="export-b@example.com")
    _make_order(db_session, user_b, product_id=product.id, order_number="ORD-B-ONLY")

    resp = await client.post("/api/v1/me/data-export", headers=_headers(user_a))
    body = resp.json()
    assert body["user"]["email"] == "export-a@example.com"
    assert body["orders"] == []


async def test_data_export_requires_auth(client):
    resp = await client.post("/api/v1/me/data-export")
    assert resp.status_code == 401


# --- account deletion ------------------------------------------------------


async def test_deletion_request_sets_flag_and_anonymizes(client, db_session, user_factory):
    user = user_factory(
        email="delete-me@example.com",
        first_name="Anna",
        last_name="Nowak",
        phone="+48111222333",
    )
    user.company_name = "Acme Sp. z o.o."
    db_session.add(user)
    db_session.commit()

    # Give them an address that must be wiped.
    await client.post(
        "/api/v1/addresses",
        json=_make_address(client, user),
        headers=_headers(user),
    )

    resp = await client.post("/api/v1/me/account-deletion-request", headers=_headers(user))
    assert resp.status_code == 200
    body = resp.json()
    assert body["deletion_requested_at"] is not None
    assert body["anonymized_at"] is not None

    db_session.refresh(user)
    assert user.deletion_requested_at is not None
    assert user.anonymized_at is not None
    assert user.email == f"deleted-user-{user.id}@anon.invalid"
    assert user.phone is None
    assert user.company_name is None
    assert user.is_active is False
    assert user.addresses == []


async def test_deletion_keeps_order_rows(client, db_session, user_factory, product_factory):
    """Order rows survive anonymization (tax retention), still FK'd to the user."""
    product = product_factory()
    user = user_factory(email="delete-orders@example.com")
    order = _make_order(db_session, user, product_id=product.id, order_number="ORD-KEEP-1")

    await client.post("/api/v1/me/account-deletion-request", headers=_headers(user))

    db_session.refresh(order)
    surviving = db_session.query(Order).filter(Order.id == order.id).one()
    assert surviving.user_id == user.id
    assert surviving.order_number == "ORD-KEEP-1"


async def test_deletion_revokes_refresh_tokens(client, db_session, user_factory):
    from datetime import UTC, datetime, timedelta

    from app.crud.refresh_token import refresh_token as crud_rt

    user = user_factory(email="delete-tokens@example.com")
    crud_rt.create(
        db_session,
        user_id=user.id,
        jti="jti-1",
        token="plain-token-1",
        expires_at=datetime.now(UTC) + timedelta(days=7),
    )
    assert crud_rt.list_active_for_user(db_session, user_id=user.id)

    await client.post("/api/v1/me/account-deletion-request", headers=_headers(user))

    assert crud_rt.list_active_for_user(db_session, user_id=user.id) == []


async def test_deletion_preserves_consent_audit_trail(client, db_session, user_factory):
    """Consent rows are part of the audit trail and survive (cascade not triggered
    since the user row itself is kept)."""
    user = user_factory(email="delete-consent@example.com")
    await client.patch(
        "/api/v1/me/preferences",
        json={"consents": {"marketing_email": True}},
        headers=_headers(user),
    )
    await client.post("/api/v1/me/account-deletion-request", headers=_headers(user))

    rows = db_session.query(UserConsent).filter(UserConsent.user_id == user.id).all()
    assert len(rows) == 1


async def test_deletion_requires_auth(client):
    resp = await client.post("/api/v1/me/account-deletion-request")
    assert resp.status_code == 401
