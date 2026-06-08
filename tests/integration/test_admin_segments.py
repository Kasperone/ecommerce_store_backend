"""Integration tests for the admin customer-segments API (/api/v1/admin/segments).

Covers authz (401/403), CRUD + audit, the ``SegmentFilters`` validation
(``min_total_spent`` requires ``currency`` → 422; ``extra="forbid"`` rejects
unknown keys → 422), and the set-based resolver correctness via the preview
endpoints: tag any/all, min/max order count, min total spent (per-currency,
cancelled-excluded), last-order window, country (default shipping address),
role, and the empty-filter "match all" semantics.

Every mutation writes an audit row (resource_type ``segment``).
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.models.address import Address
from app.models.admin_audit_event import AdminAuditEvent
from app.models.company import Company
from app.models.company_user import CompanyUser
from app.models.order import Order, OrderStatus
from app.models.segment import Segment
from app.models.tag import Tag
from app.models.user import User, UserRole

pytestmark = pytest.mark.integration


def _headers(user) -> dict:
    token, _, _ = create_access_token(subject=str(user.id))
    return {"Authorization": f"Bearer {token}"}


def _audit_count(db: Session, action: str) -> int:
    return (
        db.query(AdminAuditEvent)
        .filter(AdminAuditEvent.resource_type == "segment", AdminAuditEvent.action == action)
        .count()
    )


def _make_order(
    db: Session,
    *,
    user_id: int,
    total: float = 100.0,
    currency: str = "USD",
    status: OrderStatus = OrderStatus.PAID,
    created_at: datetime | None = None,
) -> Order:
    """Minimal valid order row attached to a user."""
    import secrets

    order = Order(
        user_id=user_id,
        order_number=f"ORD-{secrets.token_hex(6)}",
        status=status,
        subtotal=total,
        tax=0,
        shipping=0,
        total=total,
        currency=currency,
        shipping_address="1 Test St",
        shipping_city="Testville",
        shipping_postal_code="00-000",
        shipping_country="US",
    )
    if created_at is not None:
        order.created_at = created_at
    db.add(order)
    db.commit()
    db.refresh(order)
    return order


def _make_address(db: Session, *, user_id: int, country: str) -> Address:
    addr = Address(
        user_id=user_id,
        recipient_name="Test User",
        street="1 Test St",
        city="Testville",
        postal_code="00-000",
        country=country,
        is_default_shipping=True,
    )
    db.add(addr)
    db.commit()
    return addr


# ---------------------------------------------------------------------------
# authz
# ---------------------------------------------------------------------------


async def test_list_requires_admin(client, user_factory):
    customer = user_factory(email="seg-cust@example.com", role=UserRole.CUSTOMER)
    assert (await client.get("/api/v1/admin/segments/")).status_code == 401
    assert (
        await client.get("/api/v1/admin/segments/", headers=_headers(customer))
    ).status_code == 403


async def test_create_requires_admin(client, user_factory):
    customer = user_factory(email="seg-cust2@example.com", role=UserRole.CUSTOMER)
    body = {"name": "VIPs", "filters": {}}
    assert (await client.post("/api/v1/admin/segments/", json=body)).status_code == 401
    assert (
        await client.post("/api/v1/admin/segments/", json=body, headers=_headers(customer))
    ).status_code == 403


# ---------------------------------------------------------------------------
# CRUD + audit
# ---------------------------------------------------------------------------


async def test_create_and_get(client, db_session, user_factory):
    admin = user_factory(email="seg-admin1@example.com", role=UserRole.ADMIN)
    body = {
        "name": "High spenders",
        "description": "USD whales",
        "filters": {"min_total_spent": 500, "currency": "USD"},
        "is_active": True,
    }
    res = await client.post("/api/v1/admin/segments/", json=body, headers=_headers(admin))
    assert res.status_code == 201, res.text
    data = res.json()
    assert data["name"] == "High spenders"
    assert data["filters"]["min_total_spent"] == 500
    assert data["filters"]["currency"] == "USD"
    assert data["filters"]["tag_match"] == "any"
    assert _audit_count(db_session, "create") == 1

    seg_id = data["id"]
    got = await client.get(f"/api/v1/admin/segments/{seg_id}", headers=_headers(admin))
    assert got.status_code == 200
    assert got.json()["id"] == seg_id


async def test_get_404(client, user_factory):
    admin = user_factory(email="seg-admin404@example.com", role=UserRole.ADMIN)
    assert (
        await client.get("/api/v1/admin/segments/999999", headers=_headers(admin))
    ).status_code == 404


async def test_list_pagination_and_search(client, user_factory):
    admin = user_factory(email="seg-adminlist@example.com", role=UserRole.ADMIN)
    h = _headers(admin)
    await client.post("/api/v1/admin/segments/", json={"name": "Alpha", "filters": {}}, headers=h)
    await client.post("/api/v1/admin/segments/", json={"name": "Beta", "filters": {}}, headers=h)
    res = await client.get("/api/v1/admin/segments/?search=alph", headers=h)
    assert res.status_code == 200
    body = res.json()
    assert body["total"] == 1
    assert body["items"][0]["name"] == "Alpha"


async def test_update_and_audit(client, db_session, user_factory):
    admin = user_factory(email="seg-adminupd@example.com", role=UserRole.ADMIN)
    h = _headers(admin)
    created = (
        await client.post("/api/v1/admin/segments/", json={"name": "Old", "filters": {}}, headers=h)
    ).json()
    res = await client.patch(
        f"/api/v1/admin/segments/{created['id']}",
        json={"name": "New", "is_active": False},
        headers=h,
    )
    assert res.status_code == 200
    assert res.json()["name"] == "New"
    assert res.json()["is_active"] is False
    assert _audit_count(db_session, "update") == 1


async def test_update_404(client, user_factory):
    admin = user_factory(email="seg-adminupd404@example.com", role=UserRole.ADMIN)
    assert (
        await client.patch(
            "/api/v1/admin/segments/999999", json={"name": "x"}, headers=_headers(admin)
        )
    ).status_code == 404


async def test_delete_and_audit(client, db_session, user_factory):
    admin = user_factory(email="seg-admindel@example.com", role=UserRole.ADMIN)
    h = _headers(admin)
    created = (
        await client.post(
            "/api/v1/admin/segments/", json={"name": "Doomed", "filters": {}}, headers=h
        )
    ).json()
    res = await client.delete(f"/api/v1/admin/segments/{created['id']}", headers=h)
    assert res.status_code == 204
    assert _audit_count(db_session, "delete") == 1
    assert db_session.get(Segment, created["id"]) is None


async def test_delete_404(client, user_factory):
    admin = user_factory(email="seg-admindel404@example.com", role=UserRole.ADMIN)
    assert (
        await client.delete("/api/v1/admin/segments/999999", headers=_headers(admin))
    ).status_code == 404


# ---------------------------------------------------------------------------
# SegmentFilters validation
# ---------------------------------------------------------------------------


async def test_min_total_spent_requires_currency(client, user_factory):
    admin = user_factory(email="seg-adminval@example.com", role=UserRole.ADMIN)
    body = {"name": "Bad", "filters": {"min_total_spent": 100}}
    res = await client.post("/api/v1/admin/segments/", json=body, headers=_headers(admin))
    assert res.status_code == 422
    assert "currency" in res.text


async def test_extra_forbid_rejects_unknown_filter_key(client, user_factory):
    admin = user_factory(email="seg-adminval2@example.com", role=UserRole.ADMIN)
    body = {"name": "Bad", "filters": {"bogus_field": 1}}
    res = await client.post("/api/v1/admin/segments/", json=body, headers=_headers(admin))
    assert res.status_code == 422


async def test_preview_unsaved_validates_currency(client, user_factory):
    admin = user_factory(email="seg-adminval3@example.com", role=UserRole.ADMIN)
    res = await client.post(
        "/api/v1/admin/segments/preview",
        json={"min_total_spent": 50},
        headers=_headers(admin),
    )
    assert res.status_code == 422


# ---------------------------------------------------------------------------
# Resolver correctness (via POST /preview on unsaved filters)
# ---------------------------------------------------------------------------


async def _preview(client, admin, filters: dict) -> dict:
    res = await client.post("/api/v1/admin/segments/preview", json=filters, headers=_headers(admin))
    assert res.status_code == 200, res.text
    return res.json()


async def test_empty_filter_matches_all(client, db_session, user_factory):
    admin = user_factory(email="seg-empty-admin@example.com", role=UserRole.ADMIN)
    user_factory(email="seg-empty-a@example.com")
    user_factory(email="seg-empty-b@example.com")
    total_users = db_session.query(User).count()
    body = await _preview(client, admin, {})
    assert body["count"] == total_users


async def test_tag_match_any_and_all(client, db_session, user_factory):
    admin = user_factory(email="seg-tag-admin@example.com", role=UserRole.ADMIN)
    vip = Tag(name="seg-vip")
    whale = Tag(name="seg-whale")
    db_session.add_all([vip, whale])
    db_session.commit()

    u_both = user_factory(email="seg-tag-both@example.com")
    u_vip = user_factory(email="seg-tag-vip@example.com")
    u_none = user_factory(email="seg-tag-none@example.com")
    u_both.tags = [vip, whale]
    u_vip.tags = [vip]
    db_session.add_all([u_both, u_vip])
    db_session.commit()

    # "any" → both + vip-only.
    any_body = await _preview(client, admin, {"tag_ids": [vip.id, whale.id], "tag_match": "any"})
    matched = {u_both.id, u_vip.id}
    ids = {item["id"] for item in any_body["sample"]}
    assert matched.issubset(ids)
    assert u_none.id not in ids

    # "all" → only the user carrying both tags.
    all_body = await _preview(client, admin, {"tag_ids": [vip.id, whale.id], "tag_match": "all"})
    all_ids = {item["id"] for item in all_body["sample"]}
    assert u_both.id in all_ids
    assert u_vip.id not in all_ids
    assert all_body["count"] == 1


async def test_order_count_min_max(client, db_session, user_factory):
    admin = user_factory(email="seg-oc-admin@example.com", role=UserRole.ADMIN)
    u0 = user_factory(email="seg-oc-0@example.com")
    u2 = user_factory(email="seg-oc-2@example.com")
    u5 = user_factory(email="seg-oc-5@example.com")
    for _ in range(2):
        _make_order(db_session, user_id=u2.id)
    for _ in range(5):
        _make_order(db_session, user_id=u5.id)

    min2 = await _preview(client, admin, {"min_order_count": 2})
    min2_ids = {i["id"] for i in min2["sample"]}
    assert u2.id in min2_ids and u5.id in min2_ids and u0.id not in min2_ids

    max3 = await _preview(client, admin, {"min_order_count": 1, "max_order_count": 3})
    max3_ids = {i["id"] for i in max3["sample"]}
    assert u2.id in max3_ids
    assert u5.id not in max3_ids
    assert u0.id not in max3_ids


async def test_min_total_spent_per_currency_excludes_cancelled(client, db_session, user_factory):
    admin = user_factory(email="seg-spend-admin@example.com", role=UserRole.ADMIN)
    big_usd = user_factory(email="seg-spend-big@example.com")
    small_usd = user_factory(email="seg-spend-small@example.com")
    pln_only = user_factory(email="seg-spend-pln@example.com")
    cancelled = user_factory(email="seg-spend-cancel@example.com")

    _make_order(db_session, user_id=big_usd.id, total=600, currency="USD")
    _make_order(db_session, user_id=small_usd.id, total=100, currency="USD")
    _make_order(db_session, user_id=pln_only.id, total=9999, currency="PLN")
    # Cancelled USD order must NOT count toward spend.
    _make_order(
        db_session, user_id=cancelled.id, total=900, currency="USD", status=OrderStatus.CANCELLED
    )

    body = await _preview(client, admin, {"min_total_spent": 500, "currency": "USD"})
    ids = {i["id"] for i in body["sample"]}
    assert big_usd.id in ids
    assert small_usd.id not in ids
    assert pln_only.id not in ids  # PLN spend ignored for a USD filter
    assert cancelled.id not in ids  # cancelled excluded
    assert body["count"] == 1


async def test_last_order_window(client, db_session, user_factory):
    admin = user_factory(email="seg-last-admin@example.com", role=UserRole.ADMIN)
    recent = user_factory(email="seg-last-recent@example.com")
    old = user_factory(email="seg-last-old@example.com")
    now = datetime.utcnow()
    _make_order(db_session, user_id=recent.id, created_at=now - timedelta(days=1))
    _make_order(db_session, user_id=old.id, created_at=now - timedelta(days=400))

    cutoff = (now - timedelta(days=30)).date().isoformat()
    after = await _preview(client, admin, {"last_order_after": cutoff})
    after_ids = {i["id"] for i in after["sample"]}
    assert recent.id in after_ids and old.id not in after_ids

    before = await _preview(client, admin, {"last_order_before": cutoff})
    before_ids = {i["id"] for i in before["sample"]}
    assert old.id in before_ids and recent.id not in before_ids


async def test_country_filter(client, db_session, user_factory):
    admin = user_factory(email="seg-country-admin@example.com", role=UserRole.ADMIN)
    pl = user_factory(email="seg-country-pl@example.com")
    us = user_factory(email="seg-country-us@example.com")
    _make_address(db_session, user_id=pl.id, country="Poland")
    _make_address(db_session, user_id=us.id, country="United States")

    body = await _preview(client, admin, {"country": "Poland"})
    ids = {i["id"] for i in body["sample"]}
    assert pl.id in ids
    assert us.id not in ids


async def test_role_filter_customer_and_b2b(client, db_session, user_factory):
    admin = user_factory(email="seg-role-admin@example.com", role=UserRole.ADMIN)
    plain = user_factory(email="seg-role-plain@example.com", role=UserRole.CUSTOMER)
    b2b = user_factory(email="seg-role-b2b@example.com", role=UserRole.CUSTOMER)
    company = Company(name="Acme Co", status="approved")
    db_session.add(company)
    db_session.commit()
    db_session.add(
        CompanyUser(company_id=company.id, user_id=b2b.id, role="BUYER", status="active")
    )
    db_session.commit()

    b2b_body = await _preview(client, admin, {"role": "b2b"})
    b2b_ids = {i["id"] for i in b2b_body["sample"]}
    assert b2b.id in b2b_ids
    assert plain.id not in b2b_ids

    cust_body = await _preview(client, admin, {"role": "customer"})
    cust_ids = {i["id"] for i in cust_body["sample"]}
    assert plain.id in cust_ids
    # admin role excluded from the customer filter
    assert admin.id not in cust_ids


async def test_preview_saved_segment(client, db_session, user_factory):
    admin = user_factory(email="seg-saved-admin@example.com", role=UserRole.ADMIN)
    target = user_factory(email="seg-saved-target@example.com")
    _make_order(db_session, user_id=target.id, total=800, currency="EUR")
    h = _headers(admin)
    created = (
        await client.post(
            "/api/v1/admin/segments/",
            json={"name": "EUR whales", "filters": {"min_total_spent": 500, "currency": "EUR"}},
            headers=h,
        )
    ).json()
    res = await client.get(f"/api/v1/admin/segments/{created['id']}/preview", headers=h)
    assert res.status_code == 200
    ids = {i["id"] for i in res.json()["sample"]}
    assert target.id in ids


async def test_preview_saved_404(client, user_factory):
    admin = user_factory(email="seg-saved404@example.com", role=UserRole.ADMIN)
    assert (
        await client.get("/api/v1/admin/segments/999999/preview", headers=_headers(admin))
    ).status_code == 404
