"""Integration tests for the admin warehouses API (/api/v1/admin/warehouses).

Covers authz, list (incl. the migration-seeded default MAIN), create + code
uniqueness + default reassignment, update (code collision, un-default guard,
reassign default), and delete (404 / default / has-stock guards). Every mutation
writes an audit row (resource_type ``warehouse``).
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.models.admin_audit_event import AdminAuditEvent
from app.models.user import UserRole
from app.models.warehouse import StockLevel, Warehouse

pytestmark = pytest.mark.integration


def _headers(user) -> dict:
    token, _, _ = create_access_token(subject=str(user.id))
    return {"Authorization": f"Bearer {token}"}


def _audit_count(db: Session, action: str) -> int:
    return (
        db.query(AdminAuditEvent)
        .filter(AdminAuditEvent.resource_type == "warehouse", AdminAuditEvent.action == action)
        .count()
    )


# ---------------------------------------------------------------------------
# authz
# ---------------------------------------------------------------------------


async def test_list_requires_admin(client, user_factory):
    customer = user_factory(email="wh-cust@example.com", role=UserRole.CUSTOMER)
    assert (await client.get("/api/v1/admin/warehouses/")).status_code == 401
    assert (
        await client.get("/api/v1/admin/warehouses/", headers=_headers(customer))
    ).status_code == 403


async def test_create_requires_admin(client, user_factory):
    customer = user_factory(email="wh-cust2@example.com", role=UserRole.CUSTOMER)
    body = {"name": "X", "code": "X1"}
    assert (await client.post("/api/v1/admin/warehouses/", json=body)).status_code == 401
    assert (
        await client.post("/api/v1/admin/warehouses/", json=body, headers=_headers(customer))
    ).status_code == 403


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


async def test_list_includes_seeded_default(client, user_factory):
    admin = user_factory(email="wh-admin1@example.com", role=UserRole.ADMIN)
    resp = await client.get("/api/v1/admin/warehouses/", headers=_headers(admin))
    assert resp.status_code == 200
    body = resp.json()
    codes = {w["code"]: w for w in body["items"]}
    assert "MAIN" in codes
    assert codes["MAIN"]["is_default"] is True


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


async def test_create_warehouse_writes_audit(client, user_factory, db_session):
    admin = user_factory(email="wh-admin2@example.com", role=UserRole.ADMIN)
    before = _audit_count(db_session, "create")
    resp = await client.post(
        "/api/v1/admin/warehouses/",
        json={"name": "Europe", "code": "EU-1"},
        headers=_headers(admin),
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["code"] == "EU-1"
    assert body["is_default"] is False
    assert body["is_active"] is True
    assert _audit_count(db_session, "create") == before + 1


async def test_create_duplicate_code_409(client, user_factory):
    admin = user_factory(email="wh-admin3@example.com", role=UserRole.ADMIN)
    # MAIN already exists from the migration.
    resp = await client.post(
        "/api/v1/admin/warehouses/",
        json={"name": "Dupe", "code": "MAIN"},
        headers=_headers(admin),
    )
    assert resp.status_code == 409


async def test_create_default_demotes_existing(client, user_factory, db_session):
    admin = user_factory(email="wh-admin4@example.com", role=UserRole.ADMIN)
    resp = await client.post(
        "/api/v1/admin/warehouses/",
        json={"name": "New Default", "code": "ND-1", "is_default": True},
        headers=_headers(admin),
    )
    assert resp.status_code == 201
    assert resp.json()["is_default"] is True
    # Exactly one default remains, and it is the new one.
    defaults = db_session.query(Warehouse).filter(Warehouse.is_default.is_(True)).all()
    assert len(defaults) == 1
    assert defaults[0].code == "ND-1"


# ---------------------------------------------------------------------------
# get / update
# ---------------------------------------------------------------------------


async def test_get_404(client, user_factory):
    admin = user_factory(email="wh-admin5@example.com", role=UserRole.ADMIN)
    assert (
        await client.get("/api/v1/admin/warehouses/999999", headers=_headers(admin))
    ).status_code == 404


async def test_update_name_writes_audit(client, user_factory, db_session):
    admin = user_factory(email="wh-admin6@example.com", role=UserRole.ADMIN)
    created = (
        await client.post(
            "/api/v1/admin/warehouses/",
            json={"name": "Old", "code": "UPD-1"},
            headers=_headers(admin),
        )
    ).json()
    before = _audit_count(db_session, "update")
    resp = await client.patch(
        f"/api/v1/admin/warehouses/{created['id']}",
        json={"name": "Renamed"},
        headers=_headers(admin),
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "Renamed"
    assert _audit_count(db_session, "update") == before + 1


async def test_update_code_collision_409(client, user_factory):
    admin = user_factory(email="wh-admin7@example.com", role=UserRole.ADMIN)
    a = (
        await client.post(
            "/api/v1/admin/warehouses/",
            json={"name": "A", "code": "COL-A"},
            headers=_headers(admin),
        )
    ).json()
    await client.post(
        "/api/v1/admin/warehouses/",
        json={"name": "B", "code": "COL-B"},
        headers=_headers(admin),
    )
    resp = await client.patch(
        f"/api/v1/admin/warehouses/{a['id']}",
        json={"code": "COL-B"},
        headers=_headers(admin),
    )
    assert resp.status_code == 409


async def test_cannot_undefault_the_default(client, user_factory, db_session):
    admin = user_factory(email="wh-admin8@example.com", role=UserRole.ADMIN)
    default = db_session.query(Warehouse).filter(Warehouse.is_default.is_(True)).first()
    resp = await client.patch(
        f"/api/v1/admin/warehouses/{default.id}",
        json={"is_default": False},
        headers=_headers(admin),
    )
    assert resp.status_code == 400


async def test_update_set_default_reassigns(client, user_factory, db_session):
    admin = user_factory(email="wh-admin9@example.com", role=UserRole.ADMIN)
    created = (
        await client.post(
            "/api/v1/admin/warehouses/",
            json={"name": "Promote", "code": "PRO-1"},
            headers=_headers(admin),
        )
    ).json()
    resp = await client.patch(
        f"/api/v1/admin/warehouses/{created['id']}",
        json={"is_default": True},
        headers=_headers(admin),
    )
    assert resp.status_code == 200
    assert resp.json()["is_default"] is True
    defaults = db_session.query(Warehouse).filter(Warehouse.is_default.is_(True)).all()
    assert len(defaults) == 1
    assert defaults[0].code == "PRO-1"


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


async def test_delete_404(client, user_factory):
    admin = user_factory(email="wh-admin10@example.com", role=UserRole.ADMIN)
    assert (
        await client.delete("/api/v1/admin/warehouses/999999", headers=_headers(admin))
    ).status_code == 404


async def test_cannot_delete_default(client, user_factory, db_session):
    admin = user_factory(email="wh-admin11@example.com", role=UserRole.ADMIN)
    default = db_session.query(Warehouse).filter(Warehouse.is_default.is_(True)).first()
    resp = await client.delete(f"/api/v1/admin/warehouses/{default.id}", headers=_headers(admin))
    assert resp.status_code == 409


async def test_cannot_delete_warehouse_with_stock(
    client, user_factory, product_factory, db_session
):
    admin = user_factory(email="wh-admin12@example.com", role=UserRole.ADMIN)
    product = product_factory(stock=5)
    created = (
        await client.post(
            "/api/v1/admin/warehouses/",
            json={"name": "Holds", "code": "HLD-1"},
            headers=_headers(admin),
        )
    ).json()
    db_session.add(StockLevel(product_id=product.id, warehouse_id=created["id"], quantity=3))
    db_session.commit()
    resp = await client.delete(f"/api/v1/admin/warehouses/{created['id']}", headers=_headers(admin))
    assert resp.status_code == 409


async def test_delete_empty_warehouse_writes_audit(client, user_factory, db_session):
    admin = user_factory(email="wh-admin13@example.com", role=UserRole.ADMIN)
    created = (
        await client.post(
            "/api/v1/admin/warehouses/",
            json={"name": "Temp", "code": "TMP-1"},
            headers=_headers(admin),
        )
    ).json()
    before = _audit_count(db_session, "delete")
    resp = await client.delete(f"/api/v1/admin/warehouses/{created['id']}", headers=_headers(admin))
    assert resp.status_code == 204
    assert _audit_count(db_session, "delete") == before + 1
