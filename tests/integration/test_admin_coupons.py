"""Integration tests for the admin coupons API (/api/v1/admin/coupons).

Covers authz (401/403), list pagination + filters (is_active, search), create
(happy path + audit + code uppercasing + 409 dup + value/currency/date
validation), get (200/404), update (rename, 409 collision, merged-state
re-validation, audit), and delete (204 + audit, 404). Every mutation writes an
audit row (resource_type ``coupon``). Redemption is out of scope — there is no
redeem endpoint and ``used_count`` is read-only on the response.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.models.admin_audit_event import AdminAuditEvent
from app.models.coupon import Coupon
from app.models.user import UserRole
from tests.factories.coupon import CouponFactory

pytestmark = pytest.mark.integration


def _headers(user) -> dict:
    token, _, _ = create_access_token(subject=str(user.id))
    return {"Authorization": f"Bearer {token}"}


def _audit_count(db: Session, action: str) -> int:
    return (
        db.query(AdminAuditEvent)
        .filter(AdminAuditEvent.resource_type == "coupon", AdminAuditEvent.action == action)
        .count()
    )


@pytest.fixture
def coupon_factory(db_session: Session):
    """Callable creating a Coupon row via the factory (session bound per-test)."""
    CouponFactory._meta.sqlalchemy_session = db_session  # pyright: ignore[reportAttributeAccessIssue] -- factory-boy SQLAlchemyOptions attrs aren't surfaced on the FactoryOptions stub
    CouponFactory._meta.sqlalchemy_session_persistence = "commit"  # pyright: ignore[reportAttributeAccessIssue]

    def _make(**kwargs) -> Coupon:
        return CouponFactory(**kwargs)

    return _make


# ---------------------------------------------------------------------------
# authz
# ---------------------------------------------------------------------------


async def test_list_requires_admin(client, user_factory):
    customer = user_factory(email="cpn-cust@example.com", role=UserRole.CUSTOMER)
    assert (await client.get("/api/v1/admin/coupons/")).status_code == 401
    assert (
        await client.get("/api/v1/admin/coupons/", headers=_headers(customer))
    ).status_code == 403


async def test_create_requires_admin(client, user_factory):
    customer = user_factory(email="cpn-cust2@example.com", role=UserRole.CUSTOMER)
    body = {"code": "X1", "discount_type": "percent", "value": 10}
    assert (await client.post("/api/v1/admin/coupons/", json=body)).status_code == 401
    assert (
        await client.post("/api/v1/admin/coupons/", json=body, headers=_headers(customer))
    ).status_code == 403


async def test_delete_requires_admin(client, user_factory, coupon_factory):
    customer = user_factory(email="cpn-cust3@example.com", role=UserRole.CUSTOMER)
    c = coupon_factory(code="DELAUTH")
    assert (await client.delete(f"/api/v1/admin/coupons/{c.id}")).status_code == 401
    assert (
        await client.delete(f"/api/v1/admin/coupons/{c.id}", headers=_headers(customer))
    ).status_code == 403


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


async def test_create_percent_writes_audit_and_uppercases(client, user_factory, db_session):
    admin = user_factory(email="cpn-admin1@example.com", role=UserRole.ADMIN)
    before = _audit_count(db_session, "create")
    resp = await client.post(
        "/api/v1/admin/coupons/",
        json={"code": "welcome10", "discount_type": "percent", "value": 10},
        headers=_headers(admin),
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["code"] == "WELCOME10"  # normalised UPPERCASE
    assert body["discount_type"] == "percent"
    assert body["currency"] is None
    assert body["used_count"] == 0
    assert body["is_active"] is True
    assert _audit_count(db_session, "create") == before + 1


async def test_create_with_per_user_limit_round_trips(client, user_factory):
    # SEC-003 flexible coupon: per_user_limit is settable + returned, independent
    # of the global max_uses.
    admin = user_factory(email="cpn-admin-pul@example.com", role=UserRole.ADMIN)
    resp = await client.post(
        "/api/v1/admin/coupons/",
        json={
            "code": "FLEX",
            "discount_type": "percent",
            "value": 15,
            "max_uses": 1000,
            "per_user_limit": 2,
        },
        headers=_headers(admin),
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["max_uses"] == 1000
    assert body["per_user_limit"] == 2


async def test_create_rejects_zero_per_user_limit(client, user_factory):
    admin = user_factory(email="cpn-admin-pul0@example.com", role=UserRole.ADMIN)
    resp = await client.post(
        "/api/v1/admin/coupons/",
        json={"code": "BADLIM", "discount_type": "percent", "value": 10, "per_user_limit": 0},
        headers=_headers(admin),
    )
    assert resp.status_code == 422  # ge=1 in the schema


async def test_create_fixed_requires_currency(client, user_factory):
    admin = user_factory(email="cpn-admin2@example.com", role=UserRole.ADMIN)
    resp = await client.post(
        "/api/v1/admin/coupons/",
        json={"code": "FLAT5", "discount_type": "fixed", "value": 5},
        headers=_headers(admin),
    )
    assert resp.status_code == 422


async def test_create_fixed_with_currency_ok(client, user_factory):
    admin = user_factory(email="cpn-admin3@example.com", role=UserRole.ADMIN)
    resp = await client.post(
        "/api/v1/admin/coupons/",
        json={"code": "FLAT5", "discount_type": "fixed", "value": 5, "currency": "USD"},
        headers=_headers(admin),
    )
    assert resp.status_code == 201
    assert resp.json()["currency"] == "USD"


async def test_create_percent_rejects_currency(client, user_factory):
    admin = user_factory(email="cpn-admin4@example.com", role=UserRole.ADMIN)
    resp = await client.post(
        "/api/v1/admin/coupons/",
        json={"code": "PCT5", "discount_type": "percent", "value": 5, "currency": "USD"},
        headers=_headers(admin),
    )
    assert resp.status_code == 422


async def test_create_percent_value_out_of_range(client, user_factory):
    admin = user_factory(email="cpn-admin5@example.com", role=UserRole.ADMIN)
    resp = await client.post(
        "/api/v1/admin/coupons/",
        json={"code": "PCT150", "discount_type": "percent", "value": 150},
        headers=_headers(admin),
    )
    assert resp.status_code == 422


async def test_create_rejects_ends_before_starts(client, user_factory):
    admin = user_factory(email="cpn-admin6@example.com", role=UserRole.ADMIN)
    resp = await client.post(
        "/api/v1/admin/coupons/",
        json={
            "code": "WINDOW",
            "discount_type": "percent",
            "value": 10,
            "starts_at": "2026-06-10T00:00:00",
            "ends_at": "2026-06-01T00:00:00",
        },
        headers=_headers(admin),
    )
    assert resp.status_code == 422


async def test_create_duplicate_code_409(client, user_factory, coupon_factory):
    admin = user_factory(email="cpn-admin7@example.com", role=UserRole.ADMIN)
    coupon_factory(code="DUPE")
    # Lowercase request still collides — codes are stored UPPERCASE.
    resp = await client.post(
        "/api/v1/admin/coupons/",
        json={"code": "dupe", "discount_type": "percent", "value": 10},
        headers=_headers(admin),
    )
    assert resp.status_code == 409


# ---------------------------------------------------------------------------
# list / filters / pagination
# ---------------------------------------------------------------------------


async def test_list_filters_and_paginates(client, user_factory, coupon_factory):
    admin = user_factory(email="cpn-admin8@example.com", role=UserRole.ADMIN)
    coupon_factory(code="SALE-A", is_active=True)
    coupon_factory(code="SALE-B", is_active=True)
    coupon_factory(code="SALE-C", is_active=False)
    coupon_factory(code="OTHER-X", is_active=True)

    # search filter (code contains)
    resp = await client.get(
        "/api/v1/admin/coupons/", params={"search": "SALE-"}, headers=_headers(admin)
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 3
    assert {c["code"] for c in body["items"]} == {"SALE-A", "SALE-B", "SALE-C"}

    # combine search + is_active
    resp = await client.get(
        "/api/v1/admin/coupons/",
        params={"search": "SALE-", "is_active": "true"},
        headers=_headers(admin),
    )
    body = resp.json()
    assert body["total"] == 2
    assert all(c["is_active"] for c in body["items"])

    # pagination: page_size=1 over the 3 SALE- coupons → 3 pages
    resp = await client.get(
        "/api/v1/admin/coupons/",
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
    admin = user_factory(email="cpn-admin9@example.com", role=UserRole.ADMIN)
    assert (
        await client.get("/api/v1/admin/coupons/999999", headers=_headers(admin))
    ).status_code == 404


async def test_get_returns_coupon(client, user_factory, coupon_factory):
    admin = user_factory(email="cpn-admin10@example.com", role=UserRole.ADMIN)
    c = coupon_factory(code="GETME")
    resp = await client.get(f"/api/v1/admin/coupons/{c.id}", headers=_headers(admin))
    assert resp.status_code == 200
    assert resp.json()["code"] == "GETME"


async def test_update_writes_audit(client, user_factory, coupon_factory, db_session):
    admin = user_factory(email="cpn-admin11@example.com", role=UserRole.ADMIN)
    c = coupon_factory(code="UPDME", description="old")
    before = _audit_count(db_session, "update")
    resp = await client.patch(
        f"/api/v1/admin/coupons/{c.id}",
        json={"description": "new", "is_active": False},
        headers=_headers(admin),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["description"] == "new"
    assert body["is_active"] is False
    assert _audit_count(db_session, "update") == before + 1


async def test_update_404(client, user_factory):
    admin = user_factory(email="cpn-admin12@example.com", role=UserRole.ADMIN)
    resp = await client.patch(
        "/api/v1/admin/coupons/999999",
        json={"description": "x"},
        headers=_headers(admin),
    )
    assert resp.status_code == 404


async def test_update_code_collision_409(client, user_factory, coupon_factory):
    admin = user_factory(email="cpn-admin13@example.com", role=UserRole.ADMIN)
    coupon_factory(code="COL-A")
    b = coupon_factory(code="COL-B")
    resp = await client.patch(
        f"/api/v1/admin/coupons/{b.id}",
        json={"code": "col-a"},  # lowercase still collides
        headers=_headers(admin),
    )
    assert resp.status_code == 409


async def test_update_same_code_is_allowed(client, user_factory, coupon_factory):
    admin = user_factory(email="cpn-admin14@example.com", role=UserRole.ADMIN)
    c = coupon_factory(code="SAMECODE")
    resp = await client.patch(
        f"/api/v1/admin/coupons/{c.id}",
        json={"code": "samecode", "value": 20},
        headers=_headers(admin),
    )
    assert resp.status_code == 200
    assert resp.json()["value"] == 20


async def test_update_flip_to_fixed_without_currency_422(client, user_factory, coupon_factory):
    """Merged-state validation: flipping a percent coupon to fixed without
    supplying a currency must 422 (the currency-by-type rule re-checks the merge)."""
    admin = user_factory(email="cpn-admin15@example.com", role=UserRole.ADMIN)
    c = coupon_factory(code="FLIP", discount_type="percent", value=10, currency=None)
    resp = await client.patch(
        f"/api/v1/admin/coupons/{c.id}",
        json={"discount_type": "fixed"},
        headers=_headers(admin),
    )
    assert resp.status_code == 422


async def test_update_flip_to_fixed_with_currency_ok(client, user_factory, coupon_factory):
    admin = user_factory(email="cpn-admin16@example.com", role=UserRole.ADMIN)
    c = coupon_factory(code="FLIP2", discount_type="percent", value=10, currency=None)
    resp = await client.patch(
        f"/api/v1/admin/coupons/{c.id}",
        json={"discount_type": "fixed", "value": 5, "currency": "PLN"},
        headers=_headers(admin),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["discount_type"] == "fixed"
    assert body["currency"] == "PLN"


async def test_update_window_merged_validation_422(client, user_factory, coupon_factory):
    """Setting only ``ends_at`` earlier than an already-stored ``starts_at`` 422s."""
    admin = user_factory(email="cpn-admin17@example.com", role=UserRole.ADMIN)
    from datetime import datetime

    c = coupon_factory(code="WIN", starts_at=datetime(2026, 6, 10))
    resp = await client.patch(
        f"/api/v1/admin/coupons/{c.id}",
        json={"ends_at": "2026-06-01T00:00:00"},
        headers=_headers(admin),
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


async def test_delete_writes_audit(client, user_factory, coupon_factory, db_session):
    admin = user_factory(email="cpn-admin18@example.com", role=UserRole.ADMIN)
    c = coupon_factory(code="DELME")
    before = _audit_count(db_session, "delete")
    resp = await client.delete(f"/api/v1/admin/coupons/{c.id}", headers=_headers(admin))
    assert resp.status_code == 204
    assert _audit_count(db_session, "delete") == before + 1
    assert db_session.get(Coupon, c.id) is None


async def test_delete_404(client, user_factory):
    admin = user_factory(email="cpn-admin19@example.com", role=UserRole.ADMIN)
    assert (
        await client.delete("/api/v1/admin/coupons/999999", headers=_headers(admin))
    ).status_code == 404


# ---------------------------------------------------------------------------
# free_shipping kind + is_automatic flag
# ---------------------------------------------------------------------------


async def test_create_free_shipping_coupon(client, user_factory):
    """free_shipping: value 0 (omittable — defaults to 0), no currency."""
    admin = user_factory(email="cpn-admin20@example.com", role=UserRole.ADMIN)
    resp = await client.post(
        "/api/v1/admin/coupons/",
        json={"code": "shipfree", "discount_type": "free_shipping"},
        headers=_headers(admin),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["code"] == "SHIPFREE"
    assert body["discount_type"] == "free_shipping"
    assert body["value"] == 0
    assert body["currency"] is None
    assert body["is_automatic"] is False


async def test_create_free_shipping_with_value_422(client, user_factory):
    admin = user_factory(email="cpn-admin21@example.com", role=UserRole.ADMIN)
    resp = await client.post(
        "/api/v1/admin/coupons/",
        json={"code": "SHIPVAL", "discount_type": "free_shipping", "value": 5},
        headers=_headers(admin),
    )
    assert resp.status_code == 422


async def test_create_free_shipping_with_currency_422(client, user_factory):
    admin = user_factory(email="cpn-admin22@example.com", role=UserRole.ADMIN)
    resp = await client.post(
        "/api/v1/admin/coupons/",
        json={"code": "SHIPCUR", "discount_type": "free_shipping", "currency": "USD"},
        headers=_headers(admin),
    )
    assert resp.status_code == 422


async def test_create_automatic_coupon_persists_flag(client, user_factory, db_session):
    admin = user_factory(email="cpn-admin23@example.com", role=UserRole.ADMIN)
    resp = await client.post(
        "/api/v1/admin/coupons/",
        json={"code": "AUTOCREATE", "discount_type": "percent", "value": 10, "is_automatic": True},
        headers=_headers(admin),
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["is_automatic"] is True
    row = db_session.get(Coupon, body["id"])
    assert row is not None
    assert row.is_automatic is True


async def test_update_flip_fixed_to_free_shipping_without_nulling_422(
    client, user_factory, coupon_factory
):
    """Merged-state validation (the plan's §7 risk): flipping fixed →
    free_shipping must explicitly zero ``value`` and null ``currency``."""
    admin = user_factory(email="cpn-admin24@example.com", role=UserRole.ADMIN)
    c = coupon_factory(code="FLIPSHIP", discount_type="fixed", value=5, currency="USD")
    resp = await client.patch(
        f"/api/v1/admin/coupons/{c.id}",
        json={"discount_type": "free_shipping"},
        headers=_headers(admin),
    )
    assert resp.status_code == 422


async def test_update_flip_fixed_to_free_shipping_with_nulling_ok(
    client, user_factory, coupon_factory
):
    admin = user_factory(email="cpn-admin25@example.com", role=UserRole.ADMIN)
    c = coupon_factory(code="FLIPSHIP2", discount_type="fixed", value=5, currency="USD")
    resp = await client.patch(
        f"/api/v1/admin/coupons/{c.id}",
        json={"discount_type": "free_shipping", "value": 0, "currency": None},
        headers=_headers(admin),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["discount_type"] == "free_shipping"
    assert body["value"] == 0
    assert body["currency"] is None


async def test_update_toggle_is_automatic(client, user_factory, coupon_factory):
    admin = user_factory(email="cpn-admin26@example.com", role=UserRole.ADMIN)
    c = coupon_factory(code="AUTOTOGGLE", is_automatic=False)
    resp = await client.patch(
        f"/api/v1/admin/coupons/{c.id}",
        json={"is_automatic": True},
        headers=_headers(admin),
    )
    assert resp.status_code == 200
    assert resp.json()["is_automatic"] is True
