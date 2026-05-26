"""Integration tests for the admin audit-log read API (/api/v1/admin/audit-events).

Distinct from ``test_admin_audit_events.py`` (which covers the ``AuditWriter``
emitting a row per mutation). Here we cover the *read* view: authz, the
resource_type/action/resource_id filters, pagination, newest-first ordering, and
the actor-email join (incl. the null-actor case).
"""

from __future__ import annotations

import pytest

from app.core.security import create_access_token
from app.models.admin_audit_event import AdminAuditEvent
from app.models.user import UserRole

pytestmark = pytest.mark.integration


def _headers(user) -> dict:
    token, _, _ = create_access_token(subject=str(user.id))
    return {"Authorization": f"Bearer {token}"}


def _event(
    db,
    *,
    actor_id: int | None = None,
    action: str = "update",
    resource_type: str = "product",
    resource_id: int | None = 1,
    payload: dict | None = None,
) -> AdminAuditEvent:
    event = AdminAuditEvent(
        actor_id=actor_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        payload=payload if payload is not None else {"after": {"name": "x"}},
        ip="127.0.0.1",
        user_agent="pytest",
    )
    db.add(event)
    return event


# ---------------------------------------------------------------------------
# authz
# ---------------------------------------------------------------------------


async def test_requires_admin(client, user_factory):
    customer = user_factory(email="audit-cust@example.com", role=UserRole.CUSTOMER)
    assert (await client.get("/api/v1/admin/audit-events/")).status_code == 401
    assert (
        await client.get("/api/v1/admin/audit-events/", headers=_headers(customer))
    ).status_code == 403


# ---------------------------------------------------------------------------
# list: ordering + actor email + payload
# ---------------------------------------------------------------------------


async def test_list_newest_first_with_actor_email_and_payload(client, user_factory, db_session):
    admin = user_factory(email="audit-admin@example.com", role=UserRole.ADMIN)
    first = _event(db_session, actor_id=admin.id, action="create", resource_id=1)
    second = _event(db_session, actor_id=admin.id, action="update", resource_id=1)
    db_session.commit()

    resp = await client.get("/api/v1/admin/audit-events/", headers=_headers(admin))
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2

    items = body["items"]
    assert items[0]["id"] == second.id  # newest first (id desc tiebreak)
    assert items[1]["id"] == first.id
    assert items[0]["actor_email"] == "audit-admin@example.com"
    assert items[0]["resource_type"] == "product"
    assert items[0]["payload"] == {"after": {"name": "x"}}


# ---------------------------------------------------------------------------
# filters
# ---------------------------------------------------------------------------


async def test_resource_type_filter(client, user_factory, db_session):
    admin = user_factory(email="audit-rt@example.com", role=UserRole.ADMIN)
    _event(db_session, actor_id=admin.id, resource_type="product")
    _event(db_session, actor_id=admin.id, resource_type="category")
    db_session.commit()

    resp = await client.get(
        "/api/v1/admin/audit-events/?resource_type=category", headers=_headers(admin)
    )
    rows = resp.json()["items"]
    assert len(rows) == 1
    assert rows[0]["resource_type"] == "category"


async def test_action_filter(client, user_factory, db_session):
    admin = user_factory(email="audit-ac@example.com", role=UserRole.ADMIN)
    _event(db_session, actor_id=admin.id, action="create")
    _event(db_session, actor_id=admin.id, action="delete")
    db_session.commit()

    resp = await client.get("/api/v1/admin/audit-events/?action=delete", headers=_headers(admin))
    rows = resp.json()["items"]
    assert len(rows) == 1
    assert rows[0]["action"] == "delete"


async def test_resource_id_filter(client, user_factory, db_session):
    admin = user_factory(email="audit-rid@example.com", role=UserRole.ADMIN)
    _event(db_session, actor_id=admin.id, resource_id=1)
    _event(db_session, actor_id=admin.id, resource_id=2)
    db_session.commit()

    resp = await client.get("/api/v1/admin/audit-events/?resource_id=2", headers=_headers(admin))
    rows = resp.json()["items"]
    assert len(rows) == 1
    assert rows[0]["resource_id"] == 2


async def test_null_actor_serialises_email_none(client, user_factory, db_session):
    admin = user_factory(email="audit-null@example.com", role=UserRole.ADMIN)
    # An event whose actor was later deleted (actor_id SET NULL).
    _event(db_session, actor_id=None, action="delete", resource_type="customer", resource_id=9)
    db_session.commit()

    resp = await client.get(
        "/api/v1/admin/audit-events/?resource_type=customer", headers=_headers(admin)
    )
    rows = resp.json()["items"]
    assert len(rows) == 1
    assert rows[0]["actor_id"] is None
    assert rows[0]["actor_email"] is None


# ---------------------------------------------------------------------------
# pagination
# ---------------------------------------------------------------------------


async def test_pagination(client, user_factory, db_session):
    admin = user_factory(email="audit-page@example.com", role=UserRole.ADMIN)
    for i in range(25):
        _event(db_session, actor_id=admin.id, resource_id=i)
    db_session.commit()

    p1 = await client.get(
        "/api/v1/admin/audit-events/?page=1&page_size=20", headers=_headers(admin)
    )
    body1 = p1.json()
    assert body1["total"] == 25
    assert body1["pages"] == 2
    assert len(body1["items"]) == 20

    p2 = await client.get(
        "/api/v1/admin/audit-events/?page=2&page_size=20", headers=_headers(admin)
    )
    assert len(p2.json()["items"]) == 5
