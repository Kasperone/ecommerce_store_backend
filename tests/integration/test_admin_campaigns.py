"""Integration tests for the admin campaigns API (/api/v1/admin/campaigns).

Covers authz (401/403), list pagination + filters (status, audience, search),
create (happy path + audit + default status + scheduled-requires-scheduled_at
422), get (200/404), update (happy path, merged-state schedule re-validation,
audit, 404), and delete (204 + audit, 404). Every mutation writes an audit row
(resource_type ``campaign``). Sending is out of scope — there is no send endpoint
and ``sent_at`` is read-only on the response.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.models.admin_audit_event import AdminAuditEvent
from app.models.campaign import Campaign
from app.models.user import UserRole
from tests.factories.campaign import CampaignFactory

pytestmark = pytest.mark.integration


def _headers(user) -> dict:
    token, _, _ = create_access_token(subject=str(user.id))
    return {"Authorization": f"Bearer {token}"}


def _audit_count(db: Session, action: str) -> int:
    return (
        db.query(AdminAuditEvent)
        .filter(AdminAuditEvent.resource_type == "campaign", AdminAuditEvent.action == action)
        .count()
    )


@pytest.fixture
def campaign_factory(db_session: Session):
    """Callable creating a Campaign row via the factory (session bound per-test)."""
    CampaignFactory._meta.sqlalchemy_session = db_session  # pyright: ignore[reportAttributeAccessIssue] -- factory-boy SQLAlchemyOptions attrs aren't surfaced on the FactoryOptions stub
    CampaignFactory._meta.sqlalchemy_session_persistence = "commit"  # pyright: ignore[reportAttributeAccessIssue]

    def _make(**kwargs) -> Campaign:
        return CampaignFactory(**kwargs)

    return _make


# ---------------------------------------------------------------------------
# authz
# ---------------------------------------------------------------------------


async def test_list_requires_admin(client, user_factory):
    customer = user_factory(email="cmp-cust@example.com", role=UserRole.CUSTOMER)
    assert (await client.get("/api/v1/admin/campaigns/")).status_code == 401
    assert (
        await client.get("/api/v1/admin/campaigns/", headers=_headers(customer))
    ).status_code == 403


async def test_create_requires_admin(client, user_factory):
    customer = user_factory(email="cmp-cust2@example.com", role=UserRole.CUSTOMER)
    body = {"name": "X1", "subject": "Hi", "audience": "all"}
    assert (await client.post("/api/v1/admin/campaigns/", json=body)).status_code == 401
    assert (
        await client.post("/api/v1/admin/campaigns/", json=body, headers=_headers(customer))
    ).status_code == 403


async def test_delete_requires_admin(client, user_factory, campaign_factory):
    customer = user_factory(email="cmp-cust3@example.com", role=UserRole.CUSTOMER)
    c = campaign_factory(name="DELAUTH")
    assert (await client.delete(f"/api/v1/admin/campaigns/{c.id}")).status_code == 401
    assert (
        await client.delete(f"/api/v1/admin/campaigns/{c.id}", headers=_headers(customer))
    ).status_code == 403


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


async def test_create_writes_audit_and_defaults_draft(client, user_factory, db_session):
    admin = user_factory(email="cmp-admin1@example.com", role=UserRole.ADMIN)
    before = _audit_count(db_session, "create")
    resp = await client.post(
        "/api/v1/admin/campaigns/",
        json={"name": "Spring Sale", "subject": "50% off", "audience": "all"},
        headers=_headers(admin),
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["name"] == "Spring Sale"
    assert body["subject"] == "50% off"
    assert body["audience"] == "all"
    assert body["status"] == "draft"  # default
    assert body["body"] is None
    assert body["scheduled_at"] is None
    assert body["sent_at"] is None
    assert _audit_count(db_session, "create") == before + 1


async def test_create_scheduled_requires_scheduled_at(client, user_factory):
    admin = user_factory(email="cmp-admin2@example.com", role=UserRole.ADMIN)
    resp = await client.post(
        "/api/v1/admin/campaigns/",
        json={"name": "Launch", "subject": "Live", "audience": "all", "status": "scheduled"},
        headers=_headers(admin),
    )
    assert resp.status_code == 422


async def test_create_scheduled_with_scheduled_at_ok(client, user_factory):
    admin = user_factory(email="cmp-admin3@example.com", role=UserRole.ADMIN)
    resp = await client.post(
        "/api/v1/admin/campaigns/",
        json={
            "name": "Launch",
            "subject": "Live",
            "audience": "customers",
            "status": "scheduled",
            "scheduled_at": "2026-07-01T09:00:00",
        },
        headers=_headers(admin),
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "scheduled"
    assert body["scheduled_at"] == "2026-07-01T09:00:00"


async def test_create_rejects_unknown_audience(client, user_factory):
    admin = user_factory(email="cmp-admin4@example.com", role=UserRole.ADMIN)
    resp = await client.post(
        "/api/v1/admin/campaigns/",
        json={"name": "Bad", "subject": "x", "audience": "vips"},
        headers=_headers(admin),
    )
    assert resp.status_code == 422


async def test_create_rejects_unknown_status(client, user_factory):
    admin = user_factory(email="cmp-admin5@example.com", role=UserRole.ADMIN)
    resp = await client.post(
        "/api/v1/admin/campaigns/",
        json={"name": "Bad", "subject": "x", "audience": "all", "status": "archived"},
        headers=_headers(admin),
    )
    assert resp.status_code == 422


async def test_create_ignores_sent_at_from_client(client, user_factory):
    """``sent_at`` is read-only — supplying it in the body is silently ignored."""
    admin = user_factory(email="cmp-admin6@example.com", role=UserRole.ADMIN)
    resp = await client.post(
        "/api/v1/admin/campaigns/",
        json={
            "name": "TrySetSent",
            "subject": "x",
            "audience": "all",
            "sent_at": "2026-06-01T00:00:00",
        },
        headers=_headers(admin),
    )
    assert resp.status_code == 201
    assert resp.json()["sent_at"] is None


# ---------------------------------------------------------------------------
# list / filters / pagination
# ---------------------------------------------------------------------------


async def test_list_filters_and_paginates(client, user_factory, campaign_factory):
    admin = user_factory(email="cmp-admin7@example.com", role=UserRole.ADMIN)
    campaign_factory(name="SALE-A", status="draft", audience="all")
    campaign_factory(name="SALE-B", status="draft", audience="b2b")
    campaign_factory(name="SALE-C", status="sent", audience="all")
    campaign_factory(name="OTHER-X", status="draft", audience="all")

    # search filter (name contains)
    resp = await client.get(
        "/api/v1/admin/campaigns/", params={"search": "SALE-"}, headers=_headers(admin)
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 3
    assert {c["name"] for c in body["items"]} == {"SALE-A", "SALE-B", "SALE-C"}

    # combine search + status
    resp = await client.get(
        "/api/v1/admin/campaigns/",
        params={"search": "SALE-", "status": "draft"},
        headers=_headers(admin),
    )
    body = resp.json()
    assert body["total"] == 2
    assert all(c["status"] == "draft" for c in body["items"])

    # combine search + audience
    resp = await client.get(
        "/api/v1/admin/campaigns/",
        params={"search": "SALE-", "audience": "b2b"},
        headers=_headers(admin),
    )
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["name"] == "SALE-B"

    # pagination: page_size=1 over the 3 SALE- campaigns → 3 pages
    resp = await client.get(
        "/api/v1/admin/campaigns/",
        params={"search": "SALE-", "page": 1, "page_size": 1},
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
    admin = user_factory(email="cmp-admin8@example.com", role=UserRole.ADMIN)
    assert (
        await client.get("/api/v1/admin/campaigns/999999", headers=_headers(admin))
    ).status_code == 404


async def test_get_returns_campaign(client, user_factory, campaign_factory):
    admin = user_factory(email="cmp-admin9@example.com", role=UserRole.ADMIN)
    c = campaign_factory(name="GETME")
    resp = await client.get(f"/api/v1/admin/campaigns/{c.id}", headers=_headers(admin))
    assert resp.status_code == 200
    assert resp.json()["name"] == "GETME"


async def test_update_writes_audit(client, user_factory, campaign_factory, db_session):
    admin = user_factory(email="cmp-admin10@example.com", role=UserRole.ADMIN)
    c = campaign_factory(name="UPDME", subject="old")
    before = _audit_count(db_session, "update")
    resp = await client.patch(
        f"/api/v1/admin/campaigns/{c.id}",
        json={"subject": "new", "body": "Hello there"},
        headers=_headers(admin),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["subject"] == "new"
    assert body["body"] == "Hello there"
    assert _audit_count(db_session, "update") == before + 1


async def test_update_404(client, user_factory):
    admin = user_factory(email="cmp-admin11@example.com", role=UserRole.ADMIN)
    resp = await client.patch(
        "/api/v1/admin/campaigns/999999",
        json={"subject": "x"},
        headers=_headers(admin),
    )
    assert resp.status_code == 404


async def test_update_to_scheduled_without_scheduled_at_422(client, user_factory, campaign_factory):
    """Merged-state validation: flipping a draft campaign to scheduled without
    supplying a scheduled_at must 422 (the schedule rule re-checks the merge)."""
    admin = user_factory(email="cmp-admin12@example.com", role=UserRole.ADMIN)
    c = campaign_factory(name="FLIP", status="draft", scheduled_at=None)
    resp = await client.patch(
        f"/api/v1/admin/campaigns/{c.id}",
        json={"status": "scheduled"},
        headers=_headers(admin),
    )
    assert resp.status_code == 422


async def test_update_to_scheduled_with_scheduled_at_ok(client, user_factory, campaign_factory):
    admin = user_factory(email="cmp-admin13@example.com", role=UserRole.ADMIN)
    c = campaign_factory(name="FLIP2", status="draft", scheduled_at=None)
    resp = await client.patch(
        f"/api/v1/admin/campaigns/{c.id}",
        json={"status": "scheduled", "scheduled_at": "2026-07-01T09:00:00"},
        headers=_headers(admin),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "scheduled"
    assert body["scheduled_at"] == "2026-07-01T09:00:00"


async def test_update_clear_scheduled_at_on_scheduled_422(client, user_factory, campaign_factory):
    """Clearing scheduled_at on an already-scheduled campaign 422s (merged state)."""
    from datetime import datetime

    admin = user_factory(email="cmp-admin14@example.com", role=UserRole.ADMIN)
    c = campaign_factory(
        name="SCHED", status="scheduled", scheduled_at=datetime(2026, 7, 1, 9, 0, 0)
    )
    resp = await client.patch(
        f"/api/v1/admin/campaigns/{c.id}",
        json={"scheduled_at": None},
        headers=_headers(admin),
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


async def test_delete_writes_audit(client, user_factory, campaign_factory, db_session):
    admin = user_factory(email="cmp-admin15@example.com", role=UserRole.ADMIN)
    c = campaign_factory(name="DELME")
    before = _audit_count(db_session, "delete")
    resp = await client.delete(f"/api/v1/admin/campaigns/{c.id}", headers=_headers(admin))
    assert resp.status_code == 204
    assert _audit_count(db_session, "delete") == before + 1
    assert db_session.get(Campaign, c.id) is None


async def test_delete_404(client, user_factory):
    admin = user_factory(email="cmp-admin16@example.com", role=UserRole.ADMIN)
    assert (
        await client.delete("/api/v1/admin/campaigns/999999", headers=_headers(admin))
    ).status_code == 404
