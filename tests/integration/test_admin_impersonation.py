"""Integration tests for admin customer impersonation.

Covers the `POST /admin/customers/{id}/impersonate` endpoint (authz, refusals,
the issued token + audit row) and the view-only write-guard middleware exercised
end-to-end through the ASGI app.
"""

from __future__ import annotations

import pytest

from app.core.impersonation import IMPERSONATION_WRITE_BLOCKED
from app.core.security import create_access_token, decode_token
from app.models.admin_audit_event import AdminAuditEvent
from app.models.user import UserRole

pytestmark = pytest.mark.integration


def _headers(user) -> dict[str, str]:
    token, _, _ = create_access_token(subject=str(user.id))
    return {"Authorization": f"Bearer {token}"}


async def _start_impersonation(client, admin, customer) -> str:
    """Admin starts impersonation; returns the impersonation access token."""
    resp = await client.post(
        f"/api/v1/admin/customers/{customer.id}/impersonate", headers=_headers(admin)
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


async def test_impersonate_requires_auth(client, user_factory):
    customer = user_factory()
    resp = await client.post(f"/api/v1/admin/customers/{customer.id}/impersonate")
    assert resp.status_code == 401


async def test_impersonate_forbidden_for_customer(client, user_factory):
    customer = user_factory()
    other = user_factory(email="other@example.com")
    resp = await client.post(
        f"/api/v1/admin/customers/{other.id}/impersonate", headers=_headers(customer)
    )
    assert resp.status_code == 403


async def test_impersonate_unknown_customer_404(client, user_factory):
    admin = user_factory(email="imp-admin@example.com", role=UserRole.ADMIN)
    resp = await client.post("/api/v1/admin/customers/999999/impersonate", headers=_headers(admin))
    assert resp.status_code == 404


async def test_impersonate_rejects_admin_target(client, user_factory):
    admin = user_factory(email="imp-admin2@example.com", role=UserRole.ADMIN)
    other_admin = user_factory(email="imp-admin3@example.com", role=UserRole.ADMIN)
    resp = await client.post(
        f"/api/v1/admin/customers/{other_admin.id}/impersonate", headers=_headers(admin)
    )
    assert resp.status_code == 403
    assert "admin" in resp.json()["detail"].lower()


async def test_impersonate_rejects_inactive_customer(client, user_factory):
    admin = user_factory(email="imp-admin4@example.com", role=UserRole.ADMIN)
    inactive = user_factory(email="inactive@example.com", is_active=False)
    resp = await client.post(
        f"/api/v1/admin/customers/{inactive.id}/impersonate", headers=_headers(admin)
    )
    assert resp.status_code == 400


async def test_impersonate_success_returns_token_and_customer(client, user_factory):
    admin = user_factory(email="imp-admin5@example.com", role=UserRole.ADMIN)
    customer = user_factory(email="shopper@example.com")

    resp = await client.post(
        f"/api/v1/admin/customers/{customer.id}/impersonate", headers=_headers(admin)
    )
    assert resp.status_code == 200
    body = resp.json()

    assert body["token_type"] == "bearer"
    assert body["customer"] == {
        "id": customer.id,
        "email": customer.email,
        "first_name": customer.first_name,
        "last_name": customer.last_name,
    }
    decoded = decode_token(body["access_token"], expected_type="access")
    assert decoded.subject == str(customer.id)  # the token acts AS the customer
    assert decoded.impersonator_id == admin.id  # …but records the admin


async def test_impersonation_token_is_view_only(client, user_factory):
    admin = user_factory(email="imp-admin6@example.com", role=UserRole.ADMIN)
    customer = user_factory(email="viewonly@example.com")
    token = await _start_impersonation(client, admin, customer)
    auth = {"Authorization": f"Bearer {token}"}

    # Safe read resolves AS the customer.
    me = await client.get("/api/v1/auth/me", headers=auth)
    assert me.status_code == 200
    assert me.json()["email"] == customer.email

    # Any write is blocked by the guard before it reaches the route.
    put = await client.put("/api/v1/auth/me", json={"first_name": "Hacked"}, headers=auth)
    assert put.status_code == 403
    assert put.json()["detail"] == IMPERSONATION_WRITE_BLOCKED


async def test_normal_customer_write_is_not_blocked(client, user_factory):
    customer = user_factory(email="normal@example.com")
    resp = await client.put(
        "/api/v1/auth/me", json={"first_name": "Updated"}, headers=_headers(customer)
    )
    # The guard only fires on impersonation tokens — a normal customer's write goes through.
    assert resp.status_code == 200
    assert resp.json()["first_name"] == "Updated"


async def test_impersonate_records_audit_event(client, db_session, user_factory):
    admin = user_factory(email="imp-admin7@example.com", role=UserRole.ADMIN)
    customer = user_factory(email="audited@example.com")

    await _start_impersonation(client, admin, customer)

    events = (
        db_session.query(AdminAuditEvent)
        .filter_by(action="impersonate", resource_type="customer", resource_id=customer.id)
        .all()
    )
    assert len(events) == 1
    assert events[0].payload["after"]["impersonator_id"] == admin.id
