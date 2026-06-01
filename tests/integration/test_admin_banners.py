"""Integration tests for the admin banners API (/api/v1/admin/banners).

Covers authz (401/403), list pagination + filters (is_active, placement), create
(happy path + audit + placement/date validation), get (200/404), update (fields,
merged-state window re-validation, audit, 404), and delete (204 + audit, 404).
Every mutation writes an audit row (resource_type ``banner``). Rendering banners
on the storefront is out of scope — there is no public banners endpoint.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.models.admin_audit_event import AdminAuditEvent
from app.models.banner import Banner
from app.models.user import UserRole
from tests.factories.banner import BannerFactory

pytestmark = pytest.mark.integration


def _headers(user) -> dict:
    token, _, _ = create_access_token(subject=str(user.id))
    return {"Authorization": f"Bearer {token}"}


def _audit_count(db: Session, action: str) -> int:
    return (
        db.query(AdminAuditEvent)
        .filter(AdminAuditEvent.resource_type == "banner", AdminAuditEvent.action == action)
        .count()
    )


@pytest.fixture
def banner_factory(db_session: Session):
    """Callable creating a Banner row via the factory (session bound per-test)."""
    BannerFactory._meta.sqlalchemy_session = db_session  # pyright: ignore[reportAttributeAccessIssue] -- factory-boy SQLAlchemyOptions attrs aren't surfaced on the FactoryOptions stub
    BannerFactory._meta.sqlalchemy_session_persistence = "commit"  # pyright: ignore[reportAttributeAccessIssue]

    def _make(**kwargs) -> Banner:
        return BannerFactory(**kwargs)

    return _make


# ---------------------------------------------------------------------------
# authz
# ---------------------------------------------------------------------------


async def test_list_requires_admin(client, user_factory):
    customer = user_factory(email="bnr-cust@example.com", role=UserRole.CUSTOMER)
    assert (await client.get("/api/v1/admin/banners/")).status_code == 401
    assert (
        await client.get("/api/v1/admin/banners/", headers=_headers(customer))
    ).status_code == 403


async def test_create_requires_admin(client, user_factory):
    customer = user_factory(email="bnr-cust2@example.com", role=UserRole.CUSTOMER)
    body = {"title": "X", "image_url": "https://x/i.jpg", "placement": "home_hero"}
    assert (await client.post("/api/v1/admin/banners/", json=body)).status_code == 401
    assert (
        await client.post("/api/v1/admin/banners/", json=body, headers=_headers(customer))
    ).status_code == 403


async def test_delete_requires_admin(client, user_factory, banner_factory):
    customer = user_factory(email="bnr-cust3@example.com", role=UserRole.CUSTOMER)
    b = banner_factory(title="DELAUTH")
    assert (await client.delete(f"/api/v1/admin/banners/{b.id}")).status_code == 401
    assert (
        await client.delete(f"/api/v1/admin/banners/{b.id}", headers=_headers(customer))
    ).status_code == 403


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


async def test_create_writes_audit(client, user_factory, db_session):
    admin = user_factory(email="bnr-admin1@example.com", role=UserRole.ADMIN)
    before = _audit_count(db_session, "create")
    resp = await client.post(
        "/api/v1/admin/banners/",
        json={
            "title": "Summer Sale",
            "subtitle": "Up to 50% off",
            "image_url": "https://cdn.example.com/summer.jpg",
            "link_url": "https://shop.example.com/sale",
            "placement": "home_hero",
            "sort_order": 3,
        },
        headers=_headers(admin),
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["title"] == "Summer Sale"
    assert body["subtitle"] == "Up to 50% off"
    assert body["placement"] == "home_hero"
    assert body["sort_order"] == 3
    assert body["is_active"] is True
    assert _audit_count(db_session, "create") == before + 1


async def test_create_minimal_defaults(client, user_factory):
    admin = user_factory(email="bnr-admin2@example.com", role=UserRole.ADMIN)
    resp = await client.post(
        "/api/v1/admin/banners/",
        json={"title": "Bare", "image_url": "https://x/i.jpg", "placement": "promo_strip"},
        headers=_headers(admin),
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["subtitle"] is None
    assert body["link_url"] is None
    assert body["sort_order"] == 0
    assert body["is_active"] is True


async def test_create_rejects_unknown_placement(client, user_factory):
    admin = user_factory(email="bnr-admin3@example.com", role=UserRole.ADMIN)
    resp = await client.post(
        "/api/v1/admin/banners/",
        json={"title": "X", "image_url": "https://x/i.jpg", "placement": "sidebar"},
        headers=_headers(admin),
    )
    assert resp.status_code == 422


async def test_create_rejects_ends_before_starts(client, user_factory):
    admin = user_factory(email="bnr-admin4@example.com", role=UserRole.ADMIN)
    resp = await client.post(
        "/api/v1/admin/banners/",
        json={
            "title": "Window",
            "image_url": "https://x/i.jpg",
            "placement": "home_hero",
            "starts_at": "2026-06-10T00:00:00",
            "ends_at": "2026-06-01T00:00:00",
        },
        headers=_headers(admin),
    )
    assert resp.status_code == 422


async def test_create_rejects_blank_title(client, user_factory):
    admin = user_factory(email="bnr-admin5@example.com", role=UserRole.ADMIN)
    resp = await client.post(
        "/api/v1/admin/banners/",
        json={"title": "", "image_url": "https://x/i.jpg", "placement": "home_hero"},
        headers=_headers(admin),
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# list / filters / pagination
# ---------------------------------------------------------------------------


async def test_list_filters_and_paginates(client, user_factory, banner_factory):
    admin = user_factory(email="bnr-admin6@example.com", role=UserRole.ADMIN)
    banner_factory(title="HERO-A", placement="home_hero", is_active=True, sort_order=1)
    banner_factory(title="HERO-B", placement="home_hero", is_active=True, sort_order=2)
    banner_factory(title="HERO-C", placement="home_hero", is_active=False, sort_order=3)
    banner_factory(title="STRIP-X", placement="promo_strip", is_active=True)

    # placement filter
    resp = await client.get(
        "/api/v1/admin/banners/", params={"placement": "home_hero"}, headers=_headers(admin)
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 3
    assert {b["title"] for b in body["items"]} == {"HERO-A", "HERO-B", "HERO-C"}

    # combine placement + is_active
    resp = await client.get(
        "/api/v1/admin/banners/",
        params={"placement": "home_hero", "is_active": "true"},
        headers=_headers(admin),
    )
    body = resp.json()
    assert body["total"] == 2
    assert all(b["is_active"] for b in body["items"])

    # pagination: page_size=1 over the 3 home_hero banners → 3 pages
    resp = await client.get(
        "/api/v1/admin/banners/",
        params={"placement": "home_hero", "page": 1, "page_size": 1},
        headers=_headers(admin),
    )
    body = resp.json()
    assert body["total"] == 3
    assert body["pages"] == 3
    assert len(body["items"]) == 1
    assert body["page"] == 1
    assert body["page_size"] == 1
    # lowest sort_order first
    assert body["items"][0]["title"] == "HERO-A"


async def test_list_rejects_unknown_placement_filter(client, user_factory):
    admin = user_factory(email="bnr-admin7@example.com", role=UserRole.ADMIN)
    resp = await client.get(
        "/api/v1/admin/banners/", params={"placement": "sidebar"}, headers=_headers(admin)
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# get / update
# ---------------------------------------------------------------------------


async def test_get_404(client, user_factory):
    admin = user_factory(email="bnr-admin8@example.com", role=UserRole.ADMIN)
    assert (
        await client.get("/api/v1/admin/banners/999999", headers=_headers(admin))
    ).status_code == 404


async def test_get_returns_banner(client, user_factory, banner_factory):
    admin = user_factory(email="bnr-admin9@example.com", role=UserRole.ADMIN)
    b = banner_factory(title="GETME")
    resp = await client.get(f"/api/v1/admin/banners/{b.id}", headers=_headers(admin))
    assert resp.status_code == 200
    assert resp.json()["title"] == "GETME"


async def test_update_writes_audit(client, user_factory, banner_factory, db_session):
    admin = user_factory(email="bnr-admin10@example.com", role=UserRole.ADMIN)
    b = banner_factory(title="UPDME", subtitle="old")
    before = _audit_count(db_session, "update")
    resp = await client.patch(
        f"/api/v1/admin/banners/{b.id}",
        json={"subtitle": "new", "is_active": False, "sort_order": 7},
        headers=_headers(admin),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["subtitle"] == "new"
    assert body["is_active"] is False
    assert body["sort_order"] == 7
    assert _audit_count(db_session, "update") == before + 1


async def test_update_404(client, user_factory):
    admin = user_factory(email="bnr-admin11@example.com", role=UserRole.ADMIN)
    resp = await client.patch(
        "/api/v1/admin/banners/999999",
        json={"title": "x"},
        headers=_headers(admin),
    )
    assert resp.status_code == 404


async def test_update_change_placement(client, user_factory, banner_factory):
    admin = user_factory(email="bnr-admin12@example.com", role=UserRole.ADMIN)
    b = banner_factory(title="FLIP", placement="home_hero")
    resp = await client.patch(
        f"/api/v1/admin/banners/{b.id}",
        json={"placement": "promo_strip"},
        headers=_headers(admin),
    )
    assert resp.status_code == 200
    assert resp.json()["placement"] == "promo_strip"


async def test_update_window_merged_validation_422(client, user_factory, banner_factory):
    """Setting only ``ends_at`` earlier than an already-stored ``starts_at`` 422s."""
    from datetime import datetime

    admin = user_factory(email="bnr-admin13@example.com", role=UserRole.ADMIN)
    b = banner_factory(title="WIN", starts_at=datetime(2026, 6, 10))
    resp = await client.patch(
        f"/api/v1/admin/banners/{b.id}",
        json={"ends_at": "2026-06-01T00:00:00"},
        headers=_headers(admin),
    )
    assert resp.status_code == 422


async def test_update_window_merged_validation_ok(client, user_factory, banner_factory):
    """Setting only ``ends_at`` after an already-stored ``starts_at`` succeeds."""
    from datetime import datetime

    admin = user_factory(email="bnr-admin14@example.com", role=UserRole.ADMIN)
    b = banner_factory(title="WIN2", starts_at=datetime(2026, 6, 1))
    resp = await client.patch(
        f"/api/v1/admin/banners/{b.id}",
        json={"ends_at": "2026-06-30T00:00:00"},
        headers=_headers(admin),
    )
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


async def test_delete_writes_audit(client, user_factory, banner_factory, db_session):
    admin = user_factory(email="bnr-admin15@example.com", role=UserRole.ADMIN)
    b = banner_factory(title="DELME")
    before = _audit_count(db_session, "delete")
    resp = await client.delete(f"/api/v1/admin/banners/{b.id}", headers=_headers(admin))
    assert resp.status_code == 204
    assert _audit_count(db_session, "delete") == before + 1
    assert db_session.get(Banner, b.id) is None


async def test_delete_404(client, user_factory):
    admin = user_factory(email="bnr-admin16@example.com", role=UserRole.ADMIN)
    assert (
        await client.delete("/api/v1/admin/banners/999999", headers=_headers(admin))
    ).status_code == 404
