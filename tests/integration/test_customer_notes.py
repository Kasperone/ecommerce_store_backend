"""Integration tests for admin customer notes.

Covers the note sub-routes under ``/api/v1/admin/customers/{id}/notes`` — authz,
CRUD, cross-customer scoping, audit rows (resource_type ``customer_note``), and
the ``note_count`` surfaced on the customer list. Plan:
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


def _note_audit_count(db: Session, resource_id: int) -> int:
    return (
        db.query(AdminAuditEvent)
        .filter(
            AdminAuditEvent.resource_type == "customer_note",
            AdminAuditEvent.resource_id == resource_id,
        )
        .count()
    )


# ---------------------------------------------------------------------------
# authz
# ---------------------------------------------------------------------------


async def test_notes_require_admin(client, user_factory):
    customer = user_factory(email="note-authz@example.com", role=UserRole.CUSTOMER)
    url = f"/api/v1/admin/customers/{customer.id}/notes"
    assert (await client.get(url)).status_code == 401
    assert (await client.get(url, headers=_headers(customer))).status_code == 403
    assert (
        await client.post(url, json={"body": "x"}, headers=_headers(customer))
    ).status_code == 403


# ---------------------------------------------------------------------------
# create + list
# ---------------------------------------------------------------------------


async def test_create_then_list_note(client, user_factory, db_session: Session):
    admin = user_factory(
        email="note-admin@example.com", role=UserRole.ADMIN, first_name="Ada", last_name="Min"
    )
    customer = user_factory(email="note-cust@example.com")
    url = f"/api/v1/admin/customers/{customer.id}/notes"

    created = await client.post(
        url, json={"body": "VIP — handle with care"}, headers=_headers(admin)
    )
    assert created.status_code == 201
    body = created.json()
    assert body["body"] == "VIP — handle with care"
    assert body["author_id"] == admin.id
    assert body["author_name"] == "Ada M."

    listed = await client.get(url, headers=_headers(admin))
    assert listed.status_code == 200
    assert [n["body"] for n in listed.json()] == ["VIP — handle with care"]
    assert _note_audit_count(db_session, body["id"]) == 1


async def test_list_orders_newest_first(client, user_factory):
    admin = user_factory(email="order-admin@example.com", role=UserRole.ADMIN)
    customer = user_factory(email="order-cust@example.com")
    url = f"/api/v1/admin/customers/{customer.id}/notes"
    for text in ("first", "second", "third"):
        await client.post(url, json={"body": text}, headers=_headers(admin))

    listed = await client.get(url, headers=_headers(admin))
    assert [n["body"] for n in listed.json()] == ["third", "second", "first"]


async def test_create_unknown_customer_404(client, user_factory):
    admin = user_factory(email="nf-admin@example.com", role=UserRole.ADMIN)
    resp = await client.post(
        "/api/v1/admin/customers/999999/notes", json={"body": "x"}, headers=_headers(admin)
    )
    assert resp.status_code == 404


async def test_empty_body_rejected(client, user_factory):
    admin = user_factory(email="empty-admin@example.com", role=UserRole.ADMIN)
    customer = user_factory(email="empty-cust@example.com")
    resp = await client.post(
        f"/api/v1/admin/customers/{customer.id}/notes",
        json={"body": ""},
        headers=_headers(admin),
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# update + delete
# ---------------------------------------------------------------------------


async def test_update_note(client, user_factory, db_session: Session):
    admin = user_factory(email="upd-admin@example.com", role=UserRole.ADMIN)
    customer = user_factory(email="upd-cust@example.com")
    url = f"/api/v1/admin/customers/{customer.id}/notes"
    note_id = (await client.post(url, json={"body": "old"}, headers=_headers(admin))).json()["id"]

    resp = await client.patch(f"{url}/{note_id}", json={"body": "new"}, headers=_headers(admin))
    assert resp.status_code == 200
    assert resp.json()["body"] == "new"
    # one create + one update audit row for this note
    assert _note_audit_count(db_session, note_id) == 2


async def test_delete_note(client, user_factory, db_session: Session):
    admin = user_factory(email="del-admin@example.com", role=UserRole.ADMIN)
    customer = user_factory(email="del-cust@example.com")
    url = f"/api/v1/admin/customers/{customer.id}/notes"
    note_id = (await client.post(url, json={"body": "doomed"}, headers=_headers(admin))).json()[
        "id"
    ]

    resp = await client.delete(f"{url}/{note_id}", headers=_headers(admin))
    assert resp.status_code == 204
    assert (await client.get(url, headers=_headers(admin))).json() == []
    # create + delete audit rows
    assert _note_audit_count(db_session, note_id) == 2


async def test_note_scoped_to_customer(client, user_factory):
    """A note created for customer A must 404 when reached via customer B's path."""
    admin = user_factory(email="scope-admin@example.com", role=UserRole.ADMIN)
    cust_a = user_factory(email="scope-a@example.com")
    cust_b = user_factory(email="scope-b@example.com")
    note_id = (
        await client.post(
            f"/api/v1/admin/customers/{cust_a.id}/notes",
            json={"body": "A's note"},
            headers=_headers(admin),
        )
    ).json()["id"]

    wrong = f"/api/v1/admin/customers/{cust_b.id}/notes/{note_id}"
    assert (
        await client.patch(wrong, json={"body": "x"}, headers=_headers(admin))
    ).status_code == 404
    assert (await client.delete(wrong, headers=_headers(admin))).status_code == 404


# ---------------------------------------------------------------------------
# note_count on the customer list
# ---------------------------------------------------------------------------


async def test_note_count_in_customer_list(client, user_factory):
    admin = user_factory(email="count-admin@example.com", role=UserRole.ADMIN)
    customer = user_factory(email="count-cust@example.com")
    url = f"/api/v1/admin/customers/{customer.id}/notes"
    await client.post(url, json={"body": "n1"}, headers=_headers(admin))
    await client.post(url, json={"body": "n2"}, headers=_headers(admin))

    listed = await client.get("/api/v1/admin/customers/?search=count-cust", headers=_headers(admin))
    row = next(r for r in listed.json()["items"] if r["email"] == "count-cust@example.com")
    assert row["note_count"] == 2
