"""Integration tests for the admin pages API (/api/v1/admin/pages).

Covers authz (401/403/200), create (+audit, slug+locale uniqueness, layout
validation), get (200/404), draft save (validate + custom-html sanitization +
audit), publish (snapshot version + swap + public visibility + audit), versions
list + rollback, preview-token mint, and delete (+audit, cascade versions).
Every mutation writes an audit row (resource_type ``page``).
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.models.admin_audit_event import AdminAuditEvent
from app.models.page import Page, PageVersion
from app.models.user import UserRole

pytestmark = pytest.mark.integration


def _headers(user) -> dict:
    token, _, _ = create_access_token(subject=str(user.id))
    return {"Authorization": f"Bearer {token}"}


def _audit_count(db: Session, action: str) -> int:
    return (
        db.query(AdminAuditEvent)
        .filter(AdminAuditEvent.resource_type == "page", AdminAuditEvent.action == action)
        .count()
    )


async def _create_page(client, admin, *, slug, locale="en", sections=None, **extra):
    body = {
        "slug": slug,
        "type": "standard",
        "locale": locale,
        "draft_layout": {"sections": sections or []},
        **extra,
    }
    return await client.post("/api/v1/admin/pages/", json=body, headers=_headers(admin))


# ---------------------------------------------------------------------------
# authz
# ---------------------------------------------------------------------------


async def test_list_requires_admin(client, user_factory):
    customer = user_factory(email="pg-cust@example.com", role=UserRole.CUSTOMER)
    assert (await client.get("/api/v1/admin/pages/")).status_code == 401
    assert (await client.get("/api/v1/admin/pages/", headers=_headers(customer))).status_code == 403


async def test_create_requires_admin(client, user_factory):
    customer = user_factory(email="pg-cust2@example.com", role=UserRole.CUSTOMER)
    body = {"slug": "x", "draft_layout": {"sections": []}}
    assert (await client.post("/api/v1/admin/pages/", json=body)).status_code == 401
    assert (
        await client.post("/api/v1/admin/pages/", json=body, headers=_headers(customer))
    ).status_code == 403


async def test_admin_can_list(client, user_factory):
    admin = user_factory(email="pg-admin0@example.com", role=UserRole.ADMIN)
    assert (await client.get("/api/v1/admin/pages/", headers=_headers(admin))).status_code == 200


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


async def test_create_writes_audit(client, user_factory, db_session):
    admin = user_factory(email="pg-admin1@example.com", role=UserRole.ADMIN)
    before = _audit_count(db_session, "create")
    resp = await _create_page(
        client,
        admin,
        slug="landing",
        sections=[{"type": "hero", "settings": {"heading": "Hello"}}],
        seo_title="Landing",
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["slug"] == "landing"
    assert body["status"] == "draft"
    assert body["draft_layout"]["sections"][0]["settings"]["heading"] == "Hello"
    assert body["created_by"] == admin.id
    assert _audit_count(db_session, "create") == before + 1


async def test_create_rejects_unknown_block_type(client, user_factory):
    admin = user_factory(email="pg-admin2@example.com", role=UserRole.ADMIN)
    resp = await _create_page(client, admin, slug="bad", sections=[{"type": "nope"}])
    assert resp.status_code == 422


async def test_create_duplicate_slug_locale_409(client, user_factory):
    admin = user_factory(email="pg-admin3@example.com", role=UserRole.ADMIN)
    assert (await _create_page(client, admin, slug="dupe")).status_code == 201
    assert (await _create_page(client, admin, slug="dupe")).status_code == 409
    # same slug, different locale → allowed
    assert (await _create_page(client, admin, slug="dupe", locale="pl")).status_code == 201


async def test_create_sanitizes_custom_html(client, user_factory):
    admin = user_factory(email="pg-admin4@example.com", role=UserRole.ADMIN)
    resp = await _create_page(
        client,
        admin,
        slug="xss",
        sections=[
            {
                "type": "custom-html",
                "settings": {"html": "<p>ok</p><script>alert(1)</script>"},
            }
        ],
    )
    assert resp.status_code == 201
    html = resp.json()["draft_layout"]["sections"][0]["settings"]["html"]
    assert "<script>" not in html
    assert "alert(1)" not in html
    assert "<p>ok</p>" in html


# ---------------------------------------------------------------------------
# get / draft
# ---------------------------------------------------------------------------


async def test_get_404(client, user_factory):
    admin = user_factory(email="pg-admin5@example.com", role=UserRole.ADMIN)
    assert (
        await client.get("/api/v1/admin/pages/999999", headers=_headers(admin))
    ).status_code == 404


async def test_save_draft_validates_and_audits(client, user_factory, db_session):
    admin = user_factory(email="pg-admin6@example.com", role=UserRole.ADMIN)
    page_id = (await _create_page(client, admin, slug="drafted")).json()["id"]
    before = _audit_count(db_session, "update")
    resp = await client.patch(
        f"/api/v1/admin/pages/{page_id}/draft",
        json={
            "draft_layout": {"sections": [{"type": "spacer", "settings": {"height": 100}}]},
            "seo_title": "New SEO",
        },
        headers=_headers(admin),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["draft_layout"]["sections"][0]["type"] == "spacer"
    assert body["seo_title"] == "New SEO"
    assert _audit_count(db_session, "update") == before + 1


async def test_save_draft_rejects_bad_layout(client, user_factory):
    admin = user_factory(email="pg-admin7@example.com", role=UserRole.ADMIN)
    page_id = (await _create_page(client, admin, slug="draft-bad")).json()["id"]
    resp = await client.patch(
        f"/api/v1/admin/pages/{page_id}/draft",
        json={"draft_layout": {"sections": [{"type": "ghost"}]}},
        headers=_headers(admin),
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# publish + versions + rollback
# ---------------------------------------------------------------------------


async def test_publish_snapshots_and_goes_public(client, user_factory, db_session):
    admin = user_factory(email="pg-admin8@example.com", role=UserRole.ADMIN)
    page_id = (
        await _create_page(
            client,
            admin,
            slug="pubme",
            sections=[{"type": "hero", "settings": {"heading": "Live"}}],
        )
    ).json()["id"]
    before = _audit_count(db_session, "publish")

    resp = await client.post(
        f"/api/v1/admin/pages/{page_id}/publish",
        json={"label": "first"},
        headers=_headers(admin),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "published"
    assert body["published_version_id"] is not None
    assert body["published_layout"]["sections"][0]["settings"]["heading"] == "Live"
    assert _audit_count(db_session, "publish") == before + 1

    # now public
    pub = await client.get("/api/v1/pages/pubme")
    assert pub.status_code == 200
    assert pub.json()["layout"]["sections"][0]["settings"]["heading"] == "Live"

    # a version row exists
    versions = await client.get(f"/api/v1/admin/pages/{page_id}/versions", headers=_headers(admin))
    assert versions.status_code == 200
    assert versions.json()["total"] == 1


async def test_rollback_restores_prior_version(client, user_factory, db_session):
    admin = user_factory(email="pg-admin9@example.com", role=UserRole.ADMIN)
    page_id = (
        await _create_page(
            client, admin, slug="roll", sections=[{"type": "rich-text", "settings": {"body": "v1"}}]
        )
    ).json()["id"]
    # publish v1
    await client.post(f"/api/v1/admin/pages/{page_id}/publish", headers=_headers(admin))
    v1 = (
        await client.get(f"/api/v1/admin/pages/{page_id}/versions", headers=_headers(admin))
    ).json()["items"][0]["id"]

    # edit draft to v2 and publish
    await client.patch(
        f"/api/v1/admin/pages/{page_id}/draft",
        json={"draft_layout": {"sections": [{"type": "rich-text", "settings": {"body": "v2"}}]}},
        headers=_headers(admin),
    )
    await client.post(f"/api/v1/admin/pages/{page_id}/publish", headers=_headers(admin))

    # rollback to v1 → draft holds v1 body again
    before = _audit_count(db_session, "rollback")
    resp = await client.post(
        f"/api/v1/admin/pages/{page_id}/versions/{v1}/rollback", headers=_headers(admin)
    )
    assert resp.status_code == 200
    assert resp.json()["draft_layout"]["sections"][0]["settings"]["body"] == "v1"
    assert _audit_count(db_session, "rollback") == before + 1


async def test_rollback_unknown_version_404(client, user_factory):
    admin = user_factory(email="pg-admin10@example.com", role=UserRole.ADMIN)
    page_id = (await _create_page(client, admin, slug="rollnf")).json()["id"]
    resp = await client.post(
        f"/api/v1/admin/pages/{page_id}/versions/999999/rollback", headers=_headers(admin)
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# preview token mint
# ---------------------------------------------------------------------------


async def test_mint_preview_token(client, user_factory):
    admin = user_factory(email="pg-admin11@example.com", role=UserRole.ADMIN)
    page_id = (await _create_page(client, admin, slug="tok")).json()["id"]
    resp = await client.post(
        f"/api/v1/admin/pages/{page_id}/preview-token", headers=_headers(admin)
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["token"]
    assert body["expires_at"]
    # the minted token unlocks the public preview route
    prev = await client.get("/api/v1/pages/tok/preview", params={"token": body["token"]})
    assert prev.status_code == 200


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


async def test_delete_cascades_versions_and_audits(client, user_factory, db_session):
    admin = user_factory(email="pg-admin12@example.com", role=UserRole.ADMIN)
    page_id = (await _create_page(client, admin, slug="delme")).json()["id"]
    await client.post(f"/api/v1/admin/pages/{page_id}/publish", headers=_headers(admin))
    before = _audit_count(db_session, "delete")

    resp = await client.delete(f"/api/v1/admin/pages/{page_id}", headers=_headers(admin))
    assert resp.status_code == 204
    assert _audit_count(db_session, "delete") == before + 1
    assert db_session.get(Page, page_id) is None
    # versions cascaded away
    assert db_session.query(PageVersion).filter(PageVersion.page_id == page_id).count() == 0


async def test_delete_404(client, user_factory):
    admin = user_factory(email="pg-admin13@example.com", role=UserRole.ADMIN)
    assert (
        await client.delete("/api/v1/admin/pages/999999", headers=_headers(admin))
    ).status_code == 404


# ---------------------------------------------------------------------------
# list filters
# ---------------------------------------------------------------------------


async def test_list_filters_by_locale_and_status(client, user_factory):
    admin = user_factory(email="pg-admin14@example.com", role=UserRole.ADMIN)
    await _create_page(client, admin, slug="f-en", locale="en")
    await _create_page(client, admin, slug="f-pl", locale="pl")
    pub_id = (await _create_page(client, admin, slug="f-pub", locale="en")).json()["id"]
    await client.post(f"/api/v1/admin/pages/{pub_id}/publish", headers=_headers(admin))

    resp = await client.get(
        "/api/v1/admin/pages/", params={"locale": "pl"}, headers=_headers(admin)
    )
    assert resp.status_code == 200
    assert {p["slug"] for p in resp.json()["items"]} == {"f-pl"}

    resp = await client.get(
        "/api/v1/admin/pages/", params={"status": "published"}, headers=_headers(admin)
    )
    assert {p["slug"] for p in resp.json()["items"]} == {"f-pub"}
