"""Integration tests for the admin payment-terms CRUD API (net-terms tail Program A).

Covers create (per type), shape validation (net_days / due_date required for the
matching type), list, update (incl. a type change that requires its parameter),
and delete. Admin-gated behind CUSTOMERS_WRITE.
"""

from __future__ import annotations

import pytest

from app.core.security import create_access_token
from app.models.user import UserRole

pytestmark = pytest.mark.integration


def _headers(user) -> dict:
    token, _, _ = create_access_token(subject=str(user.id))
    return {"Authorization": f"Bearer {token}"}


async def test_create_net_days_term(client, user_factory):
    admin = user_factory(email="pt-admin1@example.com", role=UserRole.ADMIN)
    resp = await client.post(
        "/api/v1/admin/payment-terms/",
        json={"name": "Net 30", "term_type": "net_days", "net_days": 30},
        headers=_headers(admin),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["term_type"] == "net_days"
    assert body["net_days"] == 30
    assert body["due_date"] is None
    assert body["is_active"] is True


async def test_create_fixed_date_term(client, user_factory):
    admin = user_factory(email="pt-admin2@example.com", role=UserRole.ADMIN)
    resp = await client.post(
        "/api/v1/admin/payment-terms/",
        json={"name": "Due EOY", "term_type": "fixed_date", "due_date": "2099-12-31"},
        headers=_headers(admin),
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["due_date"] == "2099-12-31"
    assert resp.json()["net_days"] is None


async def test_create_net_days_without_days_422(client, user_factory):
    admin = user_factory(email="pt-admin3@example.com", role=UserRole.ADMIN)
    resp = await client.post(
        "/api/v1/admin/payment-terms/",
        json={"name": "Bad", "term_type": "net_days"},
        headers=_headers(admin),
    )
    assert resp.status_code == 422


async def test_list_and_update_and_delete(client, user_factory):
    admin = user_factory(email="pt-admin4@example.com", role=UserRole.ADMIN)
    created = await client.post(
        "/api/v1/admin/payment-terms/",
        json={"name": "Net 14", "term_type": "net_days", "net_days": 14},
        headers=_headers(admin),
    )
    term_id = created.json()["id"]

    listed = await client.get("/api/v1/admin/payment-terms/", headers=_headers(admin))
    assert listed.status_code == 200
    assert any(t["id"] == term_id for t in listed.json()["items"])

    # Update the day count.
    upd = await client.patch(
        f"/api/v1/admin/payment-terms/{term_id}",
        json={"net_days": 21},
        headers=_headers(admin),
    )
    assert upd.status_code == 200
    assert upd.json()["net_days"] == 21

    # Change type to fixed_date without a due_date → 422 (merged shape invalid).
    bad = await client.patch(
        f"/api/v1/admin/payment-terms/{term_id}",
        json={"term_type": "fixed_date"},
        headers=_headers(admin),
    )
    assert bad.status_code == 422

    # Change type to fixed_date WITH a due_date → ok.
    ok = await client.patch(
        f"/api/v1/admin/payment-terms/{term_id}",
        json={"term_type": "fixed_date", "due_date": "2099-01-01"},
        headers=_headers(admin),
    )
    assert ok.status_code == 200
    assert ok.json()["term_type"] == "fixed_date"

    delete = await client.delete(f"/api/v1/admin/payment-terms/{term_id}", headers=_headers(admin))
    assert delete.status_code == 204


async def test_create_requires_admin(client, user_factory):
    buyer = user_factory(email="pt-nonadmin@example.com")
    resp = await client.post(
        "/api/v1/admin/payment-terms/",
        json={"name": "Nope", "term_type": "due_on_receipt"},
        headers=_headers(buyer),
    )
    assert resp.status_code in (401, 403)
