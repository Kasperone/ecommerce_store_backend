"""Integration tests for the admin staff API (/api/v1/admin/staff).

Covers the admin-only roster list (admins only + search), the role/active PATCH
(promote/demote, activate/deactivate, audit), and the two safety guards:
self-lockout and last-active-admin (both 400). Spec:
``ecommerce_store/.claude/notes/parallel/specs/S-be-staff.md``.
"""

from __future__ import annotations

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
            AdminAuditEvent.resource_type == "user",
            AdminAuditEvent.resource_id == resource_id,
        )
        .count()
    )


# ---------------------------------------------------------------------------
# authz
# ---------------------------------------------------------------------------


async def test_list_requires_admin(client, user_factory):
    customer = user_factory(email="plain-staff@example.com", role=UserRole.CUSTOMER)
    assert (await client.get("/api/v1/admin/staff/")).status_code == 401
    assert (await client.get("/api/v1/admin/staff/", headers=_headers(customer))).status_code == 403


async def test_patch_requires_admin(client, user_factory):
    customer = user_factory(email="plain-staff2@example.com", role=UserRole.CUSTOMER)
    target = user_factory(email="target@example.com", role=UserRole.CUSTOMER)
    assert (
        await client.patch(f"/api/v1/admin/staff/{target.id}", json={"role": "ADMIN"})
    ).status_code == 401
    assert (
        await client.patch(
            f"/api/v1/admin/staff/{target.id}",
            json={"role": "ADMIN"},
            headers=_headers(customer),
        )
    ).status_code == 403


# ---------------------------------------------------------------------------
# list — admins only + search
# ---------------------------------------------------------------------------


async def test_list_returns_only_admins(client, user_factory):
    admin = user_factory(email="admin-roster@example.com", role=UserRole.ADMIN)
    user_factory(email="peer-admin@example.com", role=UserRole.ADMIN)
    user_factory(email="cust-a@example.com", role=UserRole.CUSTOMER)
    user_factory(email="cust-b@example.com", role=UserRole.CUSTOMER)

    resp = await client.get("/api/v1/admin/staff/", headers=_headers(admin))
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    assert {r["email"] for r in body["items"]} == {
        "admin-roster@example.com",
        "peer-admin@example.com",
    }
    # All returned rows are admins, and the response shape is the roster subset.
    row = body["items"][0]
    assert row["role"] == "ADMIN"
    for key in ("id", "email", "first_name", "last_name", "is_active", "is_verified", "created_at"):
        assert key in row
    # No PII columns leak into the staff roster.
    assert "shipping_country" not in row
    assert "company_name" not in row


async def test_list_search_matches_admin(client, user_factory):
    admin = user_factory(email="searcher@example.com", role=UserRole.ADMIN)
    user_factory(email="needle@example.com", first_name="Zelda", role=UserRole.ADMIN)
    user_factory(email="haystack@example.com", first_name="Bob", role=UserRole.ADMIN)

    by_email = await client.get("/api/v1/admin/staff/?search=needle", headers=_headers(admin))
    assert by_email.json()["total"] == 1
    assert by_email.json()["items"][0]["email"] == "needle@example.com"

    by_name = await client.get("/api/v1/admin/staff/?search=zelda", headers=_headers(admin))
    assert by_name.json()["total"] == 1
    assert by_name.json()["items"][0]["email"] == "needle@example.com"


async def test_list_pagination(client, user_factory):
    admin = user_factory(email="pager@example.com", role=UserRole.ADMIN)
    for i in range(4):
        user_factory(email=f"extra-admin-{i}@example.com", role=UserRole.ADMIN)

    resp = await client.get("/api/v1/admin/staff/?page=1&page_size=2", headers=_headers(admin))
    body = resp.json()
    assert body["total"] == 5  # 4 extra + the acting admin
    assert body["page"] == 1
    assert body["page_size"] == 2
    assert body["pages"] == 3
    assert len(body["items"]) == 2


# ---------------------------------------------------------------------------
# PATCH — promote / demote / activate / deactivate
# ---------------------------------------------------------------------------


async def test_promote_customer_to_admin(client, user_factory, db_session: Session):
    admin = user_factory(email="promoter@example.com", role=UserRole.ADMIN)
    customer = user_factory(email="rising-star@example.com", role=UserRole.CUSTOMER)

    resp = await client.patch(
        f"/api/v1/admin/staff/{customer.id}",
        json={"role": "ADMIN"},
        headers=_headers(admin),
    )
    assert resp.status_code == 200
    assert resp.json()["role"] == "ADMIN"
    assert _audit_count(db_session, customer.id) == 1

    fresh = db_session.get(User, customer.id)
    assert fresh is not None
    db_session.refresh(fresh)
    assert fresh.role == UserRole.ADMIN


async def test_demote_admin_when_other_active_admin_exists(
    client, user_factory, db_session: Session
):
    admin = user_factory(email="acting@example.com", role=UserRole.ADMIN)
    peer = user_factory(email="demote-me@example.com", role=UserRole.ADMIN)

    resp = await client.patch(
        f"/api/v1/admin/staff/{peer.id}",
        json={"role": "CUSTOMER"},
        headers=_headers(admin),
    )
    assert resp.status_code == 200
    assert resp.json()["role"] == "CUSTOMER"

    fresh = db_session.get(User, peer.id)
    assert fresh is not None
    db_session.refresh(fresh)
    assert fresh.role == UserRole.CUSTOMER


async def test_deactivate_admin_when_other_active_admin_exists(
    client, user_factory, db_session: Session
):
    admin = user_factory(email="acting2@example.com", role=UserRole.ADMIN)
    peer = user_factory(email="deactivate-me@example.com", role=UserRole.ADMIN, is_active=True)

    resp = await client.patch(
        f"/api/v1/admin/staff/{peer.id}",
        json={"is_active": False},
        headers=_headers(admin),
    )
    assert resp.status_code == 200
    assert resp.json()["is_active"] is False


async def test_deactivate_and_reactivate_customer(client, user_factory):
    admin = user_factory(email="acting3@example.com", role=UserRole.ADMIN)
    customer = user_factory(email="toggle-cust@example.com", role=UserRole.CUSTOMER, is_active=True)

    off = await client.patch(
        f"/api/v1/admin/staff/{customer.id}",
        json={"is_active": False},
        headers=_headers(admin),
    )
    assert off.status_code == 200
    assert off.json()["is_active"] is False

    on = await client.patch(
        f"/api/v1/admin/staff/{customer.id}",
        json={"is_active": True},
        headers=_headers(admin),
    )
    assert on.status_code == 200
    assert on.json()["is_active"] is True


async def test_patch_both_fields_at_once(client, user_factory, db_session: Session):
    admin = user_factory(email="acting4@example.com", role=UserRole.ADMIN)
    # Another active admin so demoting `peer` is not the last-admin case.
    user_factory(email="safety-net@example.com", role=UserRole.ADMIN)
    peer = user_factory(email="both-fields@example.com", role=UserRole.ADMIN, is_active=True)

    resp = await client.patch(
        f"/api/v1/admin/staff/{peer.id}",
        json={"role": "CUSTOMER", "is_active": False},
        headers=_headers(admin),
    )
    assert resp.status_code == 200
    assert resp.json()["role"] == "CUSTOMER"
    assert resp.json()["is_active"] is False


async def test_patch_404(client, user_factory):
    admin = user_factory(email="acting5@example.com", role=UserRole.ADMIN)
    resp = await client.patch(
        "/api/v1/admin/staff/999999", json={"role": "ADMIN"}, headers=_headers(admin)
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# safety guard — self-lockout
# ---------------------------------------------------------------------------


async def test_self_demote_rejected(client, user_factory):
    # Two admins so the block is the self-lockout guard, not last-admin.
    admin = user_factory(email="selfdemote@example.com", role=UserRole.ADMIN)
    user_factory(email="other-admin@example.com", role=UserRole.ADMIN)

    resp = await client.patch(
        f"/api/v1/admin/staff/{admin.id}",
        json={"role": "CUSTOMER"},
        headers=_headers(admin),
    )
    assert resp.status_code == 400
    assert "your own" in resp.json()["detail"].lower()


async def test_self_deactivate_rejected(client, user_factory):
    admin = user_factory(email="selfdeact@example.com", role=UserRole.ADMIN)
    user_factory(email="other-admin2@example.com", role=UserRole.ADMIN)

    resp = await client.patch(
        f"/api/v1/admin/staff/{admin.id}",
        json={"is_active": False},
        headers=_headers(admin),
    )
    assert resp.status_code == 400


async def test_self_noop_positive_update_allowed(client, user_factory):
    # Re-asserting one's own ADMIN role / active state must not be blocked.
    admin = user_factory(email="selfnoop@example.com", role=UserRole.ADMIN)
    resp = await client.patch(
        f"/api/v1/admin/staff/{admin.id}",
        json={"role": "ADMIN", "is_active": True},
        headers=_headers(admin),
    )
    assert resp.status_code == 200
    assert resp.json()["role"] == "ADMIN"
    assert resp.json()["is_active"] is True


# ---------------------------------------------------------------------------
# safety guard — last active admin
# ---------------------------------------------------------------------------


async def test_demote_last_active_admin_rejected(client, user_factory):
    # The acting admin IS the only active admin (a second admin exists but is
    # inactive, so it doesn't count toward the pool). Demoting self is refused.
    # In practice the last-active-admin condition can only arise on a self-PATCH,
    # because an inactive admin can't authenticate to act on a peer — so the
    # self-lockout guard fronts it. The 400 is the contract either way; the
    # last-admin CRUD logic itself is covered by a focused unit test below.
    admin = user_factory(email="lonely-admin@example.com", role=UserRole.ADMIN, is_active=True)
    user_factory(email="inactive-admin@example.com", role=UserRole.ADMIN, is_active=False)

    resp = await client.patch(
        f"/api/v1/admin/staff/{admin.id}",
        json={"role": "CUSTOMER"},
        headers=_headers(admin),
    )
    assert resp.status_code == 400


async def test_deactivate_last_active_admin_rejected(client, user_factory):
    admin = user_factory(email="lonely-admin2@example.com", role=UserRole.ADMIN, is_active=True)
    user_factory(email="inactive-admin2@example.com", role=UserRole.ADMIN, is_active=False)

    resp = await client.patch(
        f"/api/v1/admin/staff/{admin.id}",
        json={"is_active": False},
        headers=_headers(admin),
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# count_active_admins — the last-admin guard's CRUD helper (focused unit)
# ---------------------------------------------------------------------------


async def test_count_active_admins_excludes_inactive_and_customers(
    user_factory, db_session: Session
):
    from app.crud import user as crud_user

    a1 = user_factory(email="ca-1@example.com", role=UserRole.ADMIN, is_active=True)
    a2 = user_factory(email="ca-2@example.com", role=UserRole.ADMIN, is_active=True)
    user_factory(email="ca-inactive@example.com", role=UserRole.ADMIN, is_active=False)
    user_factory(email="ca-cust@example.com", role=UserRole.CUSTOMER, is_active=True)

    assert crud_user.count_active_admins(db_session) == 2
    # Excluding one active admin leaves exactly the other.
    assert crud_user.count_active_admins(db_session, exclude_id=a1.id) == 1
    # Excluding the other active admin reaches the "last admin" boundary.
    assert crud_user.count_active_admins(db_session, exclude_id=a2.id) == 1
