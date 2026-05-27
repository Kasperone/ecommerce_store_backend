"""Integration tests for customer↔tag assignment.

Covers the assignment sub-routes under ``/api/v1/admin/customers/{id}/tags`` —
assign by id / by name (get-or-create), idempotency, detach, the exactly-one
body rule, the ``tag_id`` list filter, customer-detail tags, and the
``customer_tag`` audit rows. Plan:
``ecommerce_store/.claude/notes/plans/admin/in-progress/2026-05-27-customer-notes-and-tags.md``.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.models.admin_audit_event import AdminAuditEvent
from app.models.user import UserRole

pytestmark = pytest.mark.integration


def _headers(user) -> dict:
    token, _, _ = create_access_token(subject=str(user.id))
    return {"Authorization": f"Bearer {token}"}


def _customer_tag_audits(db: Session, customer_id: int) -> int:
    return (
        db.query(AdminAuditEvent)
        .filter(
            AdminAuditEvent.resource_type == "customer_tag",
            AdminAuditEvent.resource_id == customer_id,
        )
        .count()
    )


async def _make_tag(client, admin, name: str) -> int:
    return (
        await client.post("/api/v1/admin/tags/", json={"name": name}, headers=_headers(admin))
    ).json()["id"]


async def test_assign_by_id_and_detail_shows_it(client, user_factory):
    admin = user_factory(email="asg-admin@example.com", role=UserRole.ADMIN)
    customer = user_factory(email="asg-cust@example.com")
    tag_id = await _make_tag(client, admin, "VIP")

    resp = await client.post(
        f"/api/v1/admin/customers/{customer.id}/tags",
        json={"tag_id": tag_id},
        headers=_headers(admin),
    )
    assert resp.status_code == 200
    assert [t["name"] for t in resp.json()] == ["VIP"]

    detail = await client.get(f"/api/v1/admin/customers/{customer.id}", headers=_headers(admin))
    assert [t["id"] for t in detail.json()["tags"]] == [tag_id]


async def test_assign_by_name_get_or_create_audits_both(client, user_factory, db_session: Session):
    admin = user_factory(email="goc-admin@example.com", role=UserRole.ADMIN)
    customer = user_factory(email="goc-cust@example.com")

    resp = await client.post(
        f"/api/v1/admin/customers/{customer.id}/tags",
        json={"name": "Brand New"},
        headers=_headers(admin),
    )
    assert resp.status_code == 200
    assert [t["name"] for t in resp.json()] == ["Brand New"]
    # the new tag is now in the vocabulary
    listed = await client.get("/api/v1/admin/tags/", headers=_headers(admin))
    assert "Brand New" in [t["name"] for t in listed.json()]
    # one customer_tag assignment audit row
    assert _customer_tag_audits(db_session, customer.id) == 1
    # and a tag-create audit row
    assert (
        db_session.query(AdminAuditEvent)
        .filter(AdminAuditEvent.resource_type == "tag", AdminAuditEvent.action == "create")
        .count()
        >= 1
    )


async def test_assign_idempotent(client, user_factory, db_session: Session):
    admin = user_factory(email="idem-admin@example.com", role=UserRole.ADMIN)
    customer = user_factory(email="idem-cust@example.com")
    tag_id = await _make_tag(client, admin, "repeat")
    url = f"/api/v1/admin/customers/{customer.id}/tags"

    await client.post(url, json={"tag_id": tag_id}, headers=_headers(admin))
    second = await client.post(url, json={"tag_id": tag_id}, headers=_headers(admin))
    assert [t["id"] for t in second.json()] == [tag_id]  # not duplicated
    # only the first attach recorded a customer_tag audit row
    assert _customer_tag_audits(db_session, customer.id) == 1


async def test_assign_exactly_one_required(client, user_factory):
    admin = user_factory(email="one-admin@example.com", role=UserRole.ADMIN)
    customer = user_factory(email="one-cust@example.com")
    url = f"/api/v1/admin/customers/{customer.id}/tags"
    assert (await client.post(url, json={}, headers=_headers(admin))).status_code == 422
    assert (
        await client.post(url, json={"tag_id": 1, "name": "x"}, headers=_headers(admin))
    ).status_code == 422


async def test_assign_unknown_tag_id_404(client, user_factory):
    admin = user_factory(email="unk-admin@example.com", role=UserRole.ADMIN)
    customer = user_factory(email="unk-cust@example.com")
    resp = await client.post(
        f"/api/v1/admin/customers/{customer.id}/tags",
        json={"tag_id": 999999},
        headers=_headers(admin),
    )
    assert resp.status_code == 404


async def test_detach(client, user_factory, db_session: Session):
    admin = user_factory(email="det-admin@example.com", role=UserRole.ADMIN)
    customer = user_factory(email="det-cust@example.com")
    tag_id = await _make_tag(client, admin, "drop me")
    url = f"/api/v1/admin/customers/{customer.id}/tags"
    await client.post(url, json={"tag_id": tag_id}, headers=_headers(admin))

    resp = await client.delete(f"{url}/{tag_id}", headers=_headers(admin))
    assert resp.status_code == 200
    assert resp.json() == []
    # assign (1) + detach (1) audit rows
    assert _customer_tag_audits(db_session, customer.id) == 2


async def test_detach_idempotent_and_unknown_404(client, user_factory, db_session: Session):
    admin = user_factory(email="deti-admin@example.com", role=UserRole.ADMIN)
    customer = user_factory(email="deti-cust@example.com")
    tag_id = await _make_tag(client, admin, "never assigned")
    url = f"/api/v1/admin/customers/{customer.id}/tags"

    # detaching a tag that isn't attached → 200, no audit row, no change
    resp = await client.delete(f"{url}/{tag_id}", headers=_headers(admin))
    assert resp.status_code == 200
    assert resp.json() == []
    assert _customer_tag_audits(db_session, customer.id) == 0
    # detaching a non-existent tag → 404
    assert (await client.delete(f"{url}/999999", headers=_headers(admin))).status_code == 404


async def test_tag_id_filter_on_list(client, user_factory):
    admin = user_factory(email="filt-admin@example.com", role=UserRole.ADMIN)
    tagged = user_factory(email="filt-tagged@example.com")
    user_factory(email="filt-untagged@example.com")
    tag_id = await _make_tag(client, admin, "segment")
    await client.post(
        f"/api/v1/admin/customers/{tagged.id}/tags",
        json={"tag_id": tag_id},
        headers=_headers(admin),
    )

    resp = await client.get(f"/api/v1/admin/customers/?tag_id={tag_id}", headers=_headers(admin))
    assert resp.json()["total"] == 1
    row = resp.json()["items"][0]
    assert row["email"] == "filt-tagged@example.com"
    assert [t["id"] for t in row["tags"]] == [tag_id]
