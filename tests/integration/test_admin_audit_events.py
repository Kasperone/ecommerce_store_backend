"""Integration tests for admin audit logging.

Acceptance coverage for PR0a of the admin catalog foundation plan
(2026-05-18-admin-catalog-foundation.md). Every admin POST/PUT/DELETE on
categories and products must emit exactly one ``admin_audit_events`` row in
the same transaction as the mutation.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.models.admin_audit_event import AdminAuditEvent
from app.models.category import Category
from app.models.user import UserRole


def _admin_headers(user_factory, email: str = "admin@audit.test") -> tuple[dict, int]:
    """Mint an access token for a freshly-created admin. Returns headers + id."""
    admin = user_factory(role=UserRole.ADMIN, email=email)
    token, _, _ = create_access_token(subject=str(admin.id))
    return {"Authorization": f"Bearer {token}"}, admin.id


def _audit_rows(db: Session, resource_type: str) -> list[AdminAuditEvent]:
    return (
        db.query(AdminAuditEvent)
        .filter(AdminAuditEvent.resource_type == resource_type)
        .order_by(AdminAuditEvent.id)
        .all()
    )


# ---------------------------------------------------------------------------
# Categories
# ---------------------------------------------------------------------------


async def test_create_category_writes_one_audit_row(client, db_session, user_factory):
    headers, admin_id = _admin_headers(user_factory)
    response = await client.post(
        "/api/v1/categories/",
        json={"name": "Books", "slug": "books", "description": "Printed reads"},
        headers=headers,
    )
    assert response.status_code == 201
    body = response.json()

    rows = _audit_rows(db_session, "category")
    assert len(rows) == 1
    row = rows[0]
    assert row.action == "create"
    assert row.resource_id == body["id"]
    assert row.actor_id == admin_id
    assert row.ip  # ASGI transport sets a value (127.0.0.1 under httpx)
    assert row.user_agent  # httpx sets one
    after = row.payload["after"]
    assert after["name"] == "Books"
    assert after["slug"] == "books"
    assert "before" not in row.payload


async def test_update_category_records_only_changed_fields(
    client, db_session, user_factory, category_factory
):
    headers, _ = _admin_headers(user_factory)
    cat = category_factory(name="Old", slug="old", description="orig", is_active=True)

    response = await client.put(
        f"/api/v1/categories/{cat.id}",
        json={"name": "New"},
        headers=headers,
    )
    assert response.status_code == 200

    rows = _audit_rows(db_session, "category")
    assert len(rows) == 1
    payload = rows[0].payload
    assert payload["before"] == {"name": "Old"}
    assert payload["after"] == {"name": "New"}


async def test_noop_put_writes_audit_row_with_empty_diff(
    client, db_session, user_factory, category_factory
):
    headers, _ = _admin_headers(user_factory)
    cat = category_factory(name="Same", slug="same", description="d", is_active=True)

    response = await client.put(
        f"/api/v1/categories/{cat.id}",
        json={"name": "Same", "slug": "same", "description": "d", "is_active": True},
        headers=headers,
    )
    assert response.status_code == 200

    rows = _audit_rows(db_session, "category")
    assert len(rows) == 1
    assert rows[0].action == "update"
    assert rows[0].payload == {"before": {}, "after": {}}


async def test_delete_category_records_before_snapshot(
    client, db_session, user_factory, category_factory
):
    headers, _ = _admin_headers(user_factory)
    cat = category_factory(name="Gone", slug="gone", is_active=True)

    response = await client.delete(f"/api/v1/categories/{cat.id}", headers=headers)
    assert response.status_code == 204

    rows = _audit_rows(db_session, "category")
    assert len(rows) == 1
    row = rows[0]
    assert row.action == "delete"
    assert row.resource_id == cat.id
    before = row.payload["before"]
    assert before["name"] == "Gone"
    assert before["slug"] == "gone"
    assert "after" not in row.payload


# ---------------------------------------------------------------------------
# Products
# ---------------------------------------------------------------------------


async def test_product_create_update_delete_emit_audit_rows(
    client, db_session, user_factory, category_factory
):
    headers, _ = _admin_headers(user_factory)
    cat = category_factory(name="Cat", slug="cat-prod")

    # Create
    create_resp = await client.post(
        "/api/v1/products/",
        json={
            "name": "Widget",
            "slug": "widget",
            "description": "A thing",
            "sku": "WID-001",
            "price_usd": 9.99,
            "price_pln": 39.99,
            "price_eur": 8.99,
            "stock": 5,
            "is_active": True,
            "is_featured": False,
            "category_id": cat.id,
        },
        headers=headers,
    )
    assert create_resp.status_code == 201, create_resp.text
    product_id = create_resp.json()["id"]

    # Update
    update_resp = await client.put(
        f"/api/v1/products/{product_id}",
        json={"price_usd": 14.99},
        headers=headers,
    )
    assert update_resp.status_code == 200

    # Delete
    delete_resp = await client.delete(f"/api/v1/products/{product_id}", headers=headers)
    assert delete_resp.status_code == 204

    rows = _audit_rows(db_session, "product")
    assert [r.action for r in rows] == ["create", "update", "delete"]
    assert all(r.resource_id == product_id for r in rows)

    # create row: after contains the price, no before
    assert rows[0].payload["after"]["price_usd"] == 9.99
    assert "before" not in rows[0].payload

    # update row: only the changed field
    assert rows[1].payload == {"before": {"price_usd": 9.99}, "after": {"price_usd": 14.99}}

    # delete row: before contains the snapshot at deletion time
    assert rows[2].payload["before"]["price_usd"] == 14.99
    assert "after" not in rows[2].payload


# ---------------------------------------------------------------------------
# Atomicity
# ---------------------------------------------------------------------------


async def test_commit_failure_leaves_no_audit_row(client, db_session, user_factory, monkeypatch):
    """If the route's final ``db.commit()`` raises, no audit row persists."""
    headers, _ = _admin_headers(user_factory)
    initial = db_session.query(AdminAuditEvent).count()

    fail_flag = {"on": True}
    original_commit = Session.commit

    def maybe_failing_commit(self):
        if fail_flag["on"]:
            raise RuntimeError("simulated commit failure")
        return original_commit(self)

    monkeypatch.setattr(Session, "commit", maybe_failing_commit)

    with pytest.raises(RuntimeError, match="simulated"):
        await client.post(
            "/api/v1/categories/",
            json={"name": "Boom", "slug": "boom"},
            headers=headers,
        )

    # Allow the post-test query to commit/expire so the session is usable.
    fail_flag["on"] = False
    db_session.rollback()

    assert db_session.query(AdminAuditEvent).count() == initial
    assert db_session.query(Category).filter(Category.slug == "boom").count() == 0


# ---------------------------------------------------------------------------
# Authorisation negatives — no audit row when access is denied
# ---------------------------------------------------------------------------


async def test_customer_cannot_create_category_and_no_audit_row(client, db_session, user_factory):
    customer = user_factory(email="customer@audit.test", role=UserRole.CUSTOMER)
    token, _, _ = create_access_token(subject=str(customer.id))

    initial = db_session.query(AdminAuditEvent).count()

    response = await client.post(
        "/api/v1/categories/",
        json={"name": "Nope", "slug": "nope"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 403
    assert db_session.query(AdminAuditEvent).count() == initial


async def test_missing_bearer_returns_401_and_no_audit_row(client, db_session):
    initial = db_session.query(AdminAuditEvent).count()

    response = await client.post(
        "/api/v1/categories/",
        json={"name": "Nope", "slug": "nope"},
    )
    assert response.status_code == 401
    assert db_session.query(AdminAuditEvent).count() == initial
