"""Integration tests for the category-tree route layer.

Covers ``GET /tree``, ``POST /bulk-reorder``, the ``mode=`` query param on
``DELETE /{id}``, and the cycle/depth validators on ``POST /`` + ``PUT /{id}``.
PR1 of the categories overhaul plan
(``ecommerce_store/.claude/notes/plans/admin/in-progress/2026-05-21-categories-overhaul.md``).
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.crud.category import MAX_DEPTH
from app.models.admin_audit_event import AdminAuditEvent
from app.models.category import Category
from app.models.user import UserRole

pytestmark = pytest.mark.integration


def _admin_headers(user_factory, email: str = "admin@cat-tree.test") -> dict:
    admin = user_factory(role=UserRole.ADMIN, email=email)
    token, _, _ = create_access_token(subject=str(admin.id))
    return {"Authorization": f"Bearer {token}"}


def _customer_headers(user_factory, email: str = "cust@cat-tree.test") -> dict:
    customer = user_factory(role=UserRole.CUSTOMER, email=email)
    token, _, _ = create_access_token(subject=str(customer.id))
    return {"Authorization": f"Bearer {token}"}


def _audit_count(db: Session, action: str | None = None) -> int:
    q = db.query(AdminAuditEvent).filter(AdminAuditEvent.resource_type == "category")
    if action is not None:
        q = q.filter(AdminAuditEvent.action == action)
    return q.count()


# ---------------------------------------------------------------------------
# GET /tree
# ---------------------------------------------------------------------------


async def test_tree_assembles_mixed_depth_fixture(client, category_factory):
    root_a = category_factory(slug="t-a", sort_order=0)
    root_b = category_factory(slug="t-b", sort_order=1)
    sub_a1 = category_factory(slug="t-a-1", parent_id=root_a.id, sort_order=0)
    sub_a2 = category_factory(slug="t-a-2", parent_id=root_a.id, sort_order=1)
    leaf_a1_1 = category_factory(slug="t-a-1-1", parent_id=sub_a1.id, sort_order=0)

    response = await client.get("/api/v1/categories/tree")
    assert response.status_code == 200
    body = response.json()

    by_slug = {n["slug"]: n for n in body}
    assert list(by_slug.keys()) == ["t-a", "t-b"]
    assert [c["slug"] for c in by_slug["t-a"]["children"]] == ["t-a-1", "t-a-2"]
    a1_children = next(c for c in by_slug["t-a"]["children"] if c["id"] == sub_a1.id)["children"]
    assert [c["slug"] for c in a1_children] == ["t-a-1-1"]
    assert by_slug["t-b"]["children"] == []
    assert next(c for c in body if c["slug"] == "t-a")["product_count"] == 0
    # Just to silence "unused"
    _ = (sub_a2.id, leaf_a1_1.id, root_b.id)


async def test_tree_respects_sibling_sort_order(client, category_factory):
    category_factory(slug="ord-c", sort_order=20)
    category_factory(slug="ord-a", sort_order=0)
    category_factory(slug="ord-b", sort_order=10)

    response = await client.get("/api/v1/categories/tree")
    assert [n["slug"] for n in response.json()] == ["ord-a", "ord-b", "ord-c"]


async def test_tree_filters_by_active_when_requested(client, category_factory):
    category_factory(slug="vis-1", is_active=True)
    category_factory(slug="vis-2", is_active=False)

    response = await client.get("/api/v1/categories/tree?is_active=true")
    slugs = [n["slug"] for n in response.json()]
    assert "vis-1" in slugs
    assert "vis-2" not in slugs


# ---------------------------------------------------------------------------
# POST / — parent depth validation
# ---------------------------------------------------------------------------


async def test_create_child_under_root_succeeds(client, user_factory, category_factory):
    headers = _admin_headers(user_factory)
    root = category_factory(slug="c-root")
    response = await client.post(
        "/api/v1/categories/",
        json={"name": "Child", "slug": "c-child", "parent_id": root.id},
        headers=headers,
    )
    assert response.status_code == 201
    assert response.json()["parent_id"] == root.id


async def test_create_at_depth_four_rejected(client, user_factory, category_factory):
    headers = _admin_headers(user_factory)
    root = category_factory(slug="dd-root")
    sub = category_factory(slug="dd-sub", parent_id=root.id)
    leaf = category_factory(slug="dd-leaf", parent_id=sub.id)
    response = await client.post(
        "/api/v1/categories/",
        json={"name": "TooDeep", "slug": "dd-too-deep", "parent_id": leaf.id},
        headers=headers,
    )
    assert response.status_code == 422
    assert f"depth limit ({MAX_DEPTH})" in response.json()["detail"]


async def test_create_with_missing_parent_returns_422(client, user_factory):
    headers = _admin_headers(user_factory)
    response = await client.post(
        "/api/v1/categories/",
        json={"name": "Orphan", "slug": "orphan", "parent_id": 999_999},
        headers=headers,
    )
    assert response.status_code == 422
    assert "does not exist" in response.json()["detail"]


# ---------------------------------------------------------------------------
# PUT /{id} — cycle + descendant depth validation
# ---------------------------------------------------------------------------


async def test_put_parent_to_self_returns_422(client, user_factory, category_factory):
    headers = _admin_headers(user_factory)
    a = category_factory(slug="self-parent")
    response = await client.put(
        f"/api/v1/categories/{a.id}",
        json={"parent_id": a.id},
        headers=headers,
    )
    assert response.status_code == 422
    assert "cycle" in response.json()["detail"]


async def test_put_parent_to_descendant_returns_422(client, user_factory, category_factory):
    headers = _admin_headers(user_factory)
    root = category_factory(slug="cyc-root")
    child = category_factory(slug="cyc-child", parent_id=root.id)
    response = await client.put(
        f"/api/v1/categories/{root.id}",
        json={"parent_id": child.id},
        headers=headers,
    )
    assert response.status_code == 422
    assert "cycle" in response.json()["detail"]


async def test_put_move_subtree_past_depth_cap_returns_422(client, user_factory, category_factory):
    headers = _admin_headers(user_factory)
    # X has child Y. Move X under another depth-2 node so Y lands at depth 4.
    root_a = category_factory(slug="mv-a")
    root_b = category_factory(slug="mv-b")
    inner_b = category_factory(slug="mv-b-1", parent_id=root_b.id)
    x = category_factory(slug="mv-x", parent_id=root_a.id)
    category_factory(slug="mv-y", parent_id=x.id)
    response = await client.put(
        f"/api/v1/categories/{x.id}",
        json={"parent_id": inner_b.id},
        headers=headers,
    )
    assert response.status_code == 422
    assert f"depth limit ({MAX_DEPTH})" in response.json()["detail"]


async def test_put_parent_to_none_succeeds(client, user_factory, category_factory):
    headers = _admin_headers(user_factory)
    root = category_factory(slug="lift-root")
    child = category_factory(slug="lift-child", parent_id=root.id)
    response = await client.put(
        f"/api/v1/categories/{child.id}",
        json={"parent_id": None},
        headers=headers,
    )
    assert response.status_code == 200
    assert response.json()["parent_id"] is None


# ---------------------------------------------------------------------------
# POST /bulk-reorder
# ---------------------------------------------------------------------------


async def test_bulk_reorder_happy_path(client, user_factory, category_factory, db_session):
    headers = _admin_headers(user_factory)
    a = category_factory(slug="bro-a", sort_order=0)
    b = category_factory(slug="bro-b", sort_order=1)
    response = await client.post(
        "/api/v1/categories/bulk-reorder",
        json={"items": [{"id": a.id, "sort_order": 5}, {"id": b.id, "sort_order": 1}]},
        headers=headers,
    )
    assert response.status_code == 200
    db_session.refresh(a)
    db_session.refresh(b)
    assert a.sort_order == 5
    assert b.sort_order == 1


async def test_bulk_reorder_rejects_cycle(client, user_factory, category_factory, db_session):
    headers = _admin_headers(user_factory)
    a = category_factory(slug="bro-cyc-a")
    initial = _audit_count(db_session, action="update")
    response = await client.post(
        "/api/v1/categories/bulk-reorder",
        json={"items": [{"id": a.id, "parent_id": a.id, "sort_order": 0}]},
        headers=headers,
    )
    assert response.status_code == 422
    assert _audit_count(db_session, action="update") == initial


async def test_bulk_reorder_rolls_back_on_partial_violation(
    client, user_factory, category_factory, db_session
):
    headers = _admin_headers(user_factory)
    a = category_factory(slug="bro-rb-a", sort_order=0)
    b = category_factory(slug="bro-rb-b", sort_order=0)
    response = await client.post(
        "/api/v1/categories/bulk-reorder",
        json={
            "items": [
                {"id": a.id, "sort_order": 9},
                {"id": b.id, "parent_id": b.id, "sort_order": 0},
            ]
        },
        headers=headers,
    )
    assert response.status_code == 422
    db_session.refresh(a)
    assert a.sort_order == 0


# ---------------------------------------------------------------------------
# DELETE /{id} — mode handling
# ---------------------------------------------------------------------------


async def test_delete_leaf_without_mode_succeeds(client, user_factory, category_factory):
    headers = _admin_headers(user_factory)
    leaf = category_factory(slug="del-leaf")
    response = await client.delete(f"/api/v1/categories/{leaf.id}", headers=headers)
    assert response.status_code == 204


async def test_delete_with_children_no_mode_returns_400(client, user_factory, category_factory):
    headers = _admin_headers(user_factory)
    root = category_factory(slug="del-root")
    category_factory(slug="del-root-child", parent_id=root.id)
    response = await client.delete(f"/api/v1/categories/{root.id}", headers=headers)
    assert response.status_code == 400
    assert "child categories" in response.json()["detail"]


async def test_delete_with_products_no_mode_returns_400(
    client, user_factory, category_factory, product_factory
):
    headers = _admin_headers(user_factory)
    cat = category_factory(slug="del-with-prod")
    product_factory(slug="del-prod", category=cat)
    response = await client.delete(f"/api/v1/categories/{cat.id}", headers=headers)
    assert response.status_code == 400
    assert "products" in response.json()["detail"]


async def test_delete_cascade_removes_subtree(client, user_factory, category_factory, db_session):
    headers = _admin_headers(user_factory)
    root = category_factory(slug="cas-root")
    sub = category_factory(slug="cas-sub", parent_id=root.id)
    leaf = category_factory(slug="cas-leaf", parent_id=sub.id)
    response = await client.delete(
        f"/api/v1/categories/{root.id}?mode=cascade",
        headers=headers,
    )
    assert response.status_code == 204
    surviving = [c.id for c in db_session.query(Category).all()]
    assert root.id not in surviving
    assert sub.id not in surviving
    assert leaf.id not in surviving
    assert (
        db_session.query(AdminAuditEvent)
        .filter(AdminAuditEvent.resource_type == "category")
        .filter(AdminAuditEvent.action == "delete")
        .count()
        == 3
    )


async def test_delete_cascade_with_products_rejected(
    client, user_factory, category_factory, product_factory
):
    headers = _admin_headers(user_factory)
    root = category_factory(slug="cas-prod-root")
    sub = category_factory(slug="cas-prod-sub", parent_id=root.id)
    product_factory(slug="cas-prod-p", category=sub)
    response = await client.delete(
        f"/api/v1/categories/{root.id}?mode=cascade",
        headers=headers,
    )
    assert response.status_code == 400
    assert "orphan" in response.json()["detail"]


async def test_delete_reparent_to_root_lifts_children(
    client, user_factory, category_factory, db_session
):
    headers = _admin_headers(user_factory)
    root = category_factory(slug="rep-root")
    a = category_factory(slug="rep-a", parent_id=root.id)
    b = category_factory(slug="rep-b", parent_id=root.id)
    response = await client.delete(
        f"/api/v1/categories/{root.id}?mode=reparent_to_root",
        headers=headers,
    )
    assert response.status_code == 204
    db_session.refresh(a)
    db_session.refresh(b)
    assert a.parent_id is None
    assert b.parent_id is None
    assert db_session.query(Category).filter(Category.id == root.id).first() is None


async def test_delete_reparent_to_root_still_blocks_on_products(
    client, user_factory, category_factory, product_factory
):
    headers = _admin_headers(user_factory)
    root = category_factory(slug="rep-prod-root")
    product_factory(slug="rep-prod", category=root)
    response = await client.delete(
        f"/api/v1/categories/{root.id}?mode=reparent_to_root",
        headers=headers,
    )
    assert response.status_code == 400


# ---------------------------------------------------------------------------
# GET /{id}/delete-preview
# ---------------------------------------------------------------------------


async def test_delete_preview_returns_counts(
    client, user_factory, category_factory, product_factory
):
    headers = _admin_headers(user_factory)
    root = category_factory(slug="dp-root")
    sub = category_factory(slug="dp-sub", parent_id=root.id)
    product_factory(slug="dp-direct", category=root)
    product_factory(slug="dp-sub-p", category=sub)
    response = await client.get(
        f"/api/v1/categories/{root.id}/delete-preview",
        headers=headers,
    )
    assert response.status_code == 200
    body = response.json()
    assert body == {
        "descendant_count": 1,
        "direct_product_count": 1,
        "subtree_product_count": 2,
    }


# ---------------------------------------------------------------------------
# Audit on tree mutations
# ---------------------------------------------------------------------------


async def test_bulk_reorder_writes_one_audit_per_row(
    client, user_factory, category_factory, db_session
):
    headers = _admin_headers(user_factory)
    a = category_factory(slug="aud-a", sort_order=0)
    b = category_factory(slug="aud-b", sort_order=1)
    initial = _audit_count(db_session, action="update")
    response = await client.post(
        "/api/v1/categories/bulk-reorder",
        json={"items": [{"id": a.id, "sort_order": 9}, {"id": b.id, "sort_order": 0}]},
        headers=headers,
    )
    assert response.status_code == 200
    assert _audit_count(db_session, action="update") == initial + 2


async def test_cascade_delete_writes_audit_for_each_descendant(
    client, user_factory, category_factory, db_session
):
    headers = _admin_headers(user_factory)
    root = category_factory(slug="acas-root")
    sub = category_factory(slug="acas-sub", parent_id=root.id)
    category_factory(slug="acas-leaf", parent_id=sub.id)
    initial = _audit_count(db_session, action="delete")
    response = await client.delete(
        f"/api/v1/categories/{root.id}?mode=cascade",
        headers=headers,
    )
    assert response.status_code == 204
    assert _audit_count(db_session, action="delete") == initial + 3


# ---------------------------------------------------------------------------
# Authorisation — a CUSTOMER token must be rejected on every mutating verb
# (G2: behaviour was verified live but unpinned by a test). The read-only
# GET /tree + GET /{id}/delete-preview are admin-gated too, so they belong
# here even though they don't mutate.
# ---------------------------------------------------------------------------


async def test_delete_preview_non_admin_rejected(client, user_factory, category_factory):
    category = category_factory(slug="authz-preview")
    response = await client.get(
        f"/api/v1/categories/{category.id}/delete-preview",
        headers=_customer_headers(user_factory),
    )
    assert response.status_code == 403


async def test_bulk_reorder_non_admin_rejected(client, user_factory, category_factory):
    category = category_factory(slug="authz-reorder")
    response = await client.post(
        "/api/v1/categories/bulk-reorder",
        json={"items": [{"id": category.id, "parent_id": None, "sort_order": 0}]},
        headers=_customer_headers(user_factory),
    )
    assert response.status_code == 403


async def test_create_non_admin_rejected(client, user_factory):
    response = await client.post(
        "/api/v1/categories/",
        json={"name": "Nope", "slug": "authz-create"},
        headers=_customer_headers(user_factory),
    )
    assert response.status_code == 403


async def test_update_non_admin_rejected(client, user_factory, category_factory):
    category = category_factory(slug="authz-update")
    response = await client.put(
        f"/api/v1/categories/{category.id}",
        json={"name": "Renamed"},
        headers=_customer_headers(user_factory),
    )
    assert response.status_code == 403


async def test_delete_non_admin_rejected(client, user_factory, category_factory):
    category = category_factory(slug="authz-delete")
    response = await client.delete(
        f"/api/v1/categories/{category.id}",
        headers=_customer_headers(user_factory),
    )
    assert response.status_code == 403


async def test_create_category_with_google_product_category(client, user_factory):
    headers = _admin_headers(user_factory, email="gpc-cat-create@example.com")
    response = await client.post(
        "/api/v1/categories/",
        json={"name": "GPC Category", "slug": "gpc-cat-create", "google_product_category": "5613"},
        headers=headers,
    )
    assert response.status_code == 201
    assert response.json()["google_product_category"] == "5613"


async def test_update_category_google_product_category(client, user_factory, category_factory):
    headers = _admin_headers(user_factory, email="gpc-cat-update@example.com")
    category = category_factory(slug="gpc-cat-update")
    response = await client.put(
        f"/api/v1/categories/{category.id}",
        json={"name": category.name, "slug": category.slug, "google_product_category": "2271"},
        headers=headers,
    )
    assert response.status_code == 200
    assert response.json()["google_product_category"] == "2271"
