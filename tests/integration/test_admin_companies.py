"""Integration tests for the admin companies API (/api/v1/admin/companies).

Covers authz (401/403), list pagination + filters (status, search over name and
tax_id), create (happy path + audit + default status + unknown-status 422 +
blank-name 422), get (200/404), update (happy path + audit + 404 +
unknown-status 422), and delete (204 + audit, 404). Every mutation writes an
audit row (resource_type ``company``). The response always exposes
``{ id, name, status }`` — the stable contract the quotes vertical FKs against.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.models.admin_audit_event import AdminAuditEvent
from app.models.company import Company
from app.models.user import UserRole
from tests.factories.company import CompanyFactory

pytestmark = pytest.mark.integration


def _headers(user) -> dict:
    token, _, _ = create_access_token(subject=str(user.id))
    return {"Authorization": f"Bearer {token}"}


def _audit_count(db: Session, action: str) -> int:
    return (
        db.query(AdminAuditEvent)
        .filter(AdminAuditEvent.resource_type == "company", AdminAuditEvent.action == action)
        .count()
    )


@pytest.fixture
def company_factory(db_session: Session):
    """Callable creating a Company row via the factory (session bound per-test)."""
    CompanyFactory._meta.sqlalchemy_session = db_session  # pyright: ignore[reportAttributeAccessIssue] -- factory-boy SQLAlchemyOptions attrs aren't surfaced on the FactoryOptions stub
    CompanyFactory._meta.sqlalchemy_session_persistence = "commit"  # pyright: ignore[reportAttributeAccessIssue]

    def _make(**kwargs) -> Company:
        return CompanyFactory(**kwargs)

    return _make


# ---------------------------------------------------------------------------
# authz
# ---------------------------------------------------------------------------


async def test_list_requires_admin(client, user_factory):
    customer = user_factory(email="co-cust@example.com", role=UserRole.CUSTOMER)
    assert (await client.get("/api/v1/admin/companies/")).status_code == 401
    assert (
        await client.get("/api/v1/admin/companies/", headers=_headers(customer))
    ).status_code == 403


async def test_create_requires_admin(client, user_factory):
    customer = user_factory(email="co-cust2@example.com", role=UserRole.CUSTOMER)
    body = {"name": "Acme"}
    assert (await client.post("/api/v1/admin/companies/", json=body)).status_code == 401
    assert (
        await client.post("/api/v1/admin/companies/", json=body, headers=_headers(customer))
    ).status_code == 403


async def test_delete_requires_admin(client, user_factory, company_factory):
    customer = user_factory(email="co-cust3@example.com", role=UserRole.CUSTOMER)
    c = company_factory(name="DELAUTH")
    assert (await client.delete(f"/api/v1/admin/companies/{c.id}")).status_code == 401
    assert (
        await client.delete(f"/api/v1/admin/companies/{c.id}", headers=_headers(customer))
    ).status_code == 403


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


async def test_create_writes_audit_and_defaults_pending(client, user_factory, db_session):
    admin = user_factory(email="co-admin1@example.com", role=UserRole.ADMIN)
    before = _audit_count(db_session, "create")
    resp = await client.post(
        "/api/v1/admin/companies/",
        json={"name": "Acme Corp"},
        headers=_headers(admin),
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["name"] == "Acme Corp"
    assert body["status"] == "pending"  # default
    assert body["tax_id"] is None
    assert body["email"] is None
    assert body["phone"] is None
    assert body["account_manager"] is None
    assert body["notes"] is None
    assert "id" in body
    assert _audit_count(db_session, "create") == before + 1


async def test_create_with_all_fields(client, user_factory):
    admin = user_factory(email="co-admin2@example.com", role=UserRole.ADMIN)
    resp = await client.post(
        "/api/v1/admin/companies/",
        json={
            "name": "Globex",
            "tax_id": "PL1234567890",
            "email": "contact@globex.example",
            "phone": "+48 22 000 0000",
            "status": "approved",
            "account_manager": "Jane Doe",
            "notes": "Key B2B account",
        },
        headers=_headers(admin),
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "approved"
    assert body["tax_id"] == "PL1234567890"
    assert body["account_manager"] == "Jane Doe"


async def test_create_rejects_unknown_status(client, user_factory):
    admin = user_factory(email="co-admin3@example.com", role=UserRole.ADMIN)
    resp = await client.post(
        "/api/v1/admin/companies/",
        json={"name": "Bad", "status": "archived"},
        headers=_headers(admin),
    )
    assert resp.status_code == 422


async def test_create_rejects_blank_name(client, user_factory):
    admin = user_factory(email="co-admin4@example.com", role=UserRole.ADMIN)
    resp = await client.post(
        "/api/v1/admin/companies/",
        json={"name": "   "},
        headers=_headers(admin),
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# list / filters / pagination
# ---------------------------------------------------------------------------


async def test_list_filters_and_paginates(client, user_factory, company_factory):
    admin = user_factory(email="co-admin5@example.com", role=UserRole.ADMIN)
    company_factory(name="BIZ-A", status="pending", tax_id="PL100")
    company_factory(name="BIZ-B", status="approved", tax_id="PL200")
    company_factory(name="BIZ-C", status="rejected", tax_id="PL300")
    company_factory(name="OTHER-X", status="pending")

    # search filter (name contains)
    resp = await client.get(
        "/api/v1/admin/companies/", params={"search": "BIZ-"}, headers=_headers(admin)
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 3
    assert {c["name"] for c in body["items"]} == {"BIZ-A", "BIZ-B", "BIZ-C"}

    # combine search + status
    resp = await client.get(
        "/api/v1/admin/companies/",
        params={"search": "BIZ-", "status": "approved"},
        headers=_headers(admin),
    )
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["name"] == "BIZ-B"

    # search matches tax_id too
    resp = await client.get(
        "/api/v1/admin/companies/", params={"search": "PL300"}, headers=_headers(admin)
    )
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["name"] == "BIZ-C"

    # pagination: page_size=1 over the 3 BIZ- companies → 3 pages
    resp = await client.get(
        "/api/v1/admin/companies/",
        params={"search": "BIZ-", "page": 1, "page_size": 1},
        headers=_headers(admin),
    )
    body = resp.json()
    assert body["total"] == 3
    assert body["pages"] == 3
    assert len(body["items"]) == 1
    assert body["page"] == 1
    assert body["page_size"] == 1


# ---------------------------------------------------------------------------
# get / update
# ---------------------------------------------------------------------------


async def test_get_404(client, user_factory):
    admin = user_factory(email="co-admin6@example.com", role=UserRole.ADMIN)
    assert (
        await client.get("/api/v1/admin/companies/999999", headers=_headers(admin))
    ).status_code == 404


async def test_get_returns_company(client, user_factory, company_factory):
    admin = user_factory(email="co-admin7@example.com", role=UserRole.ADMIN)
    c = company_factory(name="GETME")
    resp = await client.get(f"/api/v1/admin/companies/{c.id}", headers=_headers(admin))
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "GETME"
    assert body["id"] == c.id
    assert body["status"] == "pending"


async def test_update_writes_audit(client, user_factory, company_factory, db_session):
    admin = user_factory(email="co-admin8@example.com", role=UserRole.ADMIN)
    c = company_factory(name="UPDME", status="pending")
    before = _audit_count(db_session, "update")
    resp = await client.patch(
        f"/api/v1/admin/companies/{c.id}",
        json={"status": "approved", "account_manager": "Sam Lee"},
        headers=_headers(admin),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "approved"
    assert body["account_manager"] == "Sam Lee"
    assert _audit_count(db_session, "update") == before + 1


async def test_update_404(client, user_factory):
    admin = user_factory(email="co-admin9@example.com", role=UserRole.ADMIN)
    resp = await client.patch(
        "/api/v1/admin/companies/999999",
        json={"status": "approved"},
        headers=_headers(admin),
    )
    assert resp.status_code == 404


async def test_update_rejects_unknown_status(client, user_factory, company_factory):
    admin = user_factory(email="co-admin10@example.com", role=UserRole.ADMIN)
    c = company_factory(name="BADSTATUS")
    resp = await client.patch(
        f"/api/v1/admin/companies/{c.id}",
        json={"status": "frozen"},
        headers=_headers(admin),
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


async def test_delete_writes_audit(client, user_factory, company_factory, db_session):
    admin = user_factory(email="co-admin11@example.com", role=UserRole.ADMIN)
    c = company_factory(name="DELME")
    before = _audit_count(db_session, "delete")
    resp = await client.delete(f"/api/v1/admin/companies/{c.id}", headers=_headers(admin))
    assert resp.status_code == 204
    assert _audit_count(db_session, "delete") == before + 1
    assert db_session.get(Company, c.id) is None


async def test_delete_404(client, user_factory):
    admin = user_factory(email="co-admin12@example.com", role=UserRole.ADMIN)
    assert (
        await client.delete("/api/v1/admin/companies/999999", headers=_headers(admin))
    ).status_code == 404
