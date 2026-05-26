"""Integration tests for the admin customers API (/api/v1/admin/customers).

Covers list + filters, detail with order metrics, the verify/active PATCH (with
self-lockout guard), unlock, and the password-reset trigger. Plan:
``ecommerce_store/.claude/notes/plans/admin/in-progress/2026-05-26-admin-customers.md``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.models.admin_audit_event import AdminAuditEvent
from app.models.user import User, UserRole

pytestmark = pytest.mark.integration


def _headers(user) -> dict:
    token, _, _ = create_access_token(subject=str(user.id))
    return {"Authorization": f"Bearer {token}"}


def _audit_count(db: Session, resource_id: int) -> int:
    return (
        db.query(AdminAuditEvent)
        .filter(
            AdminAuditEvent.resource_type == "customer",
            AdminAuditEvent.resource_id == resource_id,
        )
        .count()
    )


# ---------------------------------------------------------------------------
# authz
# ---------------------------------------------------------------------------


async def test_list_requires_admin(client, user_factory):
    customer = user_factory(email="plain@example.com", role=UserRole.CUSTOMER)
    assert (await client.get("/api/v1/admin/customers/")).status_code == 401
    assert (
        await client.get("/api/v1/admin/customers/", headers=_headers(customer))
    ).status_code == 403


# ---------------------------------------------------------------------------
# list + filters
# ---------------------------------------------------------------------------


async def test_list_and_role_filter(client, user_factory):
    admin = user_factory(email="admin@example.com", role=UserRole.ADMIN)
    user_factory(email="cust-a@example.com", role=UserRole.CUSTOMER)
    user_factory(email="cust-b@example.com", role=UserRole.CUSTOMER)

    all_resp = await client.get("/api/v1/admin/customers/", headers=_headers(admin))
    assert all_resp.status_code == 200
    assert all_resp.json()["total"] == 3

    cust_resp = await client.get("/api/v1/admin/customers/?role=CUSTOMER", headers=_headers(admin))
    assert cust_resp.json()["total"] == 2
    row = cust_resp.json()["items"][0]
    assert "is_locked" in row and "order_count" in row


async def test_list_search_and_status_filters(client, user_factory):
    admin = user_factory(email="admin2@example.com", role=UserRole.ADMIN)
    user_factory(email="findme@example.com", first_name="Zoe", is_active=False)

    by_search = await client.get("/api/v1/admin/customers/?search=zoe", headers=_headers(admin))
    assert by_search.json()["total"] == 1
    assert by_search.json()["items"][0]["email"] == "findme@example.com"

    inactive = await client.get("/api/v1/admin/customers/?is_active=false", headers=_headers(admin))
    assert "findme@example.com" in [r["email"] for r in inactive.json()["items"]]


# ---------------------------------------------------------------------------
# detail + metrics
# ---------------------------------------------------------------------------


async def test_detail_includes_order_metrics(client, user_factory, product_factory):
    admin = user_factory(email="admin3@example.com", role=UserRole.ADMIN)
    customer = user_factory(email="buyer@example.com")
    product = product_factory(stock=50, price_usd=10.0)
    await client.post(
        "/api/v1/orders/",
        json={
            "items": [{"product_id": product.id, "quantity": 2}],
            "currency": "USD",
            "shipping_address": "1 St",
            "shipping_city": "Town",
            "shipping_postal_code": "00-001",
            "shipping_country": "Poland",
        },
        headers=_headers(customer),
    )

    resp = await client.get(f"/api/v1/admin/customers/{customer.id}", headers=_headers(admin))
    assert resp.status_code == 200
    body = resp.json()
    assert body["email"] == "buyer@example.com"
    assert body["metrics"]["order_count"] == 1
    assert body["metrics"]["total_spent"]["USD"] == 20.0
    assert body["metrics"]["last_order_at"] is not None


async def test_detail_404(client, user_factory):
    admin = user_factory(email="admin4@example.com", role=UserRole.ADMIN)
    resp = await client.get("/api/v1/admin/customers/999999", headers=_headers(admin))
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# PATCH verify / active
# ---------------------------------------------------------------------------


async def test_patch_toggles_flags_and_audits(client, user_factory, db_session: Session):
    admin = user_factory(email="admin5@example.com", role=UserRole.ADMIN)
    customer = user_factory(email="toggle@example.com", is_verified=False, is_active=True)

    resp = await client.patch(
        f"/api/v1/admin/customers/{customer.id}",
        json={"is_verified": True, "is_active": False},
        headers=_headers(admin),
    )
    assert resp.status_code == 200
    assert resp.json()["is_verified"] is True
    assert resp.json()["is_active"] is False
    assert _audit_count(db_session, customer.id) == 1


async def test_patch_self_disable_rejected(client, user_factory):
    admin = user_factory(email="self@example.com", role=UserRole.ADMIN)
    resp = await client.patch(
        f"/api/v1/admin/customers/{admin.id}",
        json={"is_active": False},
        headers=_headers(admin),
    )
    assert resp.status_code == 400


async def test_patch_self_verify_true_allowed(client, user_factory):
    # A no-op / positive self update must still be allowed.
    admin = user_factory(email="self2@example.com", role=UserRole.ADMIN)
    resp = await client.patch(
        f"/api/v1/admin/customers/{admin.id}",
        json={"is_verified": True},
        headers=_headers(admin),
    )
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# unlock
# ---------------------------------------------------------------------------


async def test_unlock_clears_lockout(client, user_factory, db_session: Session):
    admin = user_factory(email="admin6@example.com", role=UserRole.ADMIN)
    locked = user_factory(email="locked2@example.com")
    locked.failed_login_attempts = 10
    locked.locked_until = datetime.now(UTC).replace(tzinfo=None) + timedelta(minutes=30)
    db_session.add(locked)
    db_session.commit()

    resp = await client.post(f"/api/v1/admin/customers/{locked.id}/unlock", headers=_headers(admin))
    assert resp.status_code == 200
    assert resp.json()["is_locked"] is False

    fresh = db_session.get(User, locked.id)
    assert fresh is not None
    db_session.refresh(fresh)
    assert fresh.locked_until is None
    assert fresh.failed_login_attempts == 0


# ---------------------------------------------------------------------------
# password-reset trigger
# ---------------------------------------------------------------------------


async def test_password_reset_triggers_email_without_leaking_token(
    client, user_factory, db_session: Session, captured_emails
):
    admin = user_factory(email="admin7@example.com", role=UserRole.ADMIN)
    customer = user_factory(email="reset@example.com")

    resp = await client.post(
        f"/api/v1/admin/customers/{customer.id}/password-reset", headers=_headers(admin)
    )
    assert resp.status_code == 200
    # Generic message; never the token.
    body = resp.json()
    assert "token" not in body
    assert "message" in body

    # A reset email was sent and a token hash was persisted.
    assert any(e.get("kind") == "password_reset" for e in captured_emails)
    fresh = db_session.get(User, customer.id)
    assert fresh is not None
    db_session.refresh(fresh)
    assert fresh.password_reset_token_hash is not None

    # Audit row recorded, with no token in the payload.
    rows = (
        db_session.query(AdminAuditEvent)
        .filter(
            AdminAuditEvent.resource_type == "customer",
            AdminAuditEvent.resource_id == customer.id,
        )
        .all()
    )
    assert len(rows) == 1
    assert "token" not in str(rows[0].payload)
