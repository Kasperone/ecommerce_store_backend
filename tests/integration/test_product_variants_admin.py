"""Integration tests for the admin option/variant API
(/api/v1/products/{id}/options + /variants).

Covers authz, the matrix PUT happy path (options/values/variants created,
stock seeded through the ledger, §0 transition), audit rows, per-variant
Omnibus rows, the Meili sync hook, single-variant PATCH/DELETE incl. the
last-active-variant 409, and the public product detail carrying
``options``/``variants``/``has_variants`` (active only).
"""

from __future__ import annotations

import pytest

from app.core.security import create_access_token
from app.models.admin_audit_event import AdminAuditEvent
from app.models.inventory_movement import InventoryMovement
from app.models.price_history import PriceHistory
from app.models.user import UserRole

pytestmark = pytest.mark.integration


def _headers(user) -> dict:
    token, _, _ = create_access_token(subject=str(user.id))
    return {"Authorization": f"Bearer {token}"}


def _matrix_body() -> dict:
    return {
        "options": [
            {
                "name": "Color",
                "name_pl": "Kolor",
                "position": 0,
                "values": [
                    {"value": "Red", "value_pl": "Czerwony", "position": 0},
                    {"value": "Blue", "position": 1},
                ],
            },
            {"name": "Size", "position": 1, "values": [{"value": "XL", "position": 0}]},
        ],
        "variants": [
            {
                "option_values": ["Red", "XL"],
                "sku": "API-RED-XL",
                "price_usd": 25.0,
                "initial_stock": 4,
            },
            {"option_values": ["Blue", "XL"], "initial_stock": 6},
        ],
    }


async def _put_matrix(client, admin, product_id: int, body: dict | None = None):
    return await client.put(
        f"/api/v1/products/{product_id}/options",
        json=body if body is not None else _matrix_body(),
        headers=_headers(admin),
    )


# ---------------------------------------------------------------------------
# authz
# ---------------------------------------------------------------------------


async def test_variant_routes_require_admin(client, user_factory, product_factory):
    customer = user_factory(email="var-cust@example.com", role=UserRole.CUSTOMER)
    product = product_factory(stock=0)

    list_url = f"/api/v1/products/{product.id}/variants"
    matrix_url = f"/api/v1/products/{product.id}/options"
    assert (await client.get(list_url)).status_code == 401
    assert (await client.get(list_url, headers=_headers(customer))).status_code == 403
    assert (await client.put(matrix_url, json=_matrix_body())).status_code == 401
    assert (
        await client.put(matrix_url, json=_matrix_body(), headers=_headers(customer))
    ).status_code == 403
    variant_url = f"/api/v1/products/{product.id}/variants/1"
    assert (await client.patch(variant_url, json={})).status_code == 401
    assert (await client.patch(variant_url, json={}, headers=_headers(customer))).status_code == 403
    assert (await client.delete(variant_url)).status_code == 401
    assert (await client.delete(variant_url, headers=_headers(customer))).status_code == 403


# ---------------------------------------------------------------------------
# Matrix PUT
# ---------------------------------------------------------------------------


async def test_matrix_put_happy_path(client, db_session, user_factory, product_factory):
    admin = user_factory(email="var-admin@example.com", role=UserRole.ADMIN)
    product = product_factory(stock=7)

    resp = await _put_matrix(client, admin, product.id)
    assert resp.status_code == 200, resp.text
    data = resp.json()

    assert [option["name"] for option in data["options"]] == ["Color", "Size"]
    assert [value["value"] for value in data["options"][0]["values"]] == ["Red", "Blue"]
    assert len(data["variants"]) == 2
    by_title = {variant["title"]: variant for variant in data["variants"]}
    assert by_title["Red / XL"]["sku"] == "API-RED-XL"
    assert by_title["Red / XL"]["price_usd"] == 25.0
    assert by_title["Red / XL"]["stock"] == 4
    assert by_title["Blue / XL"]["price_usd"] is None  # inherit, raw column in admin view
    assert by_title["Blue / XL"]["stock"] == 6

    # §0 transition: the old product-grain stock (7) was zeroed; total = 4 + 6.
    db_session.refresh(product)
    assert product.stock == 10
    migration = (
        db_session.query(InventoryMovement)
        .filter_by(product_id=product.id, note="migrated to variants")
        .all()
    )
    assert [m.delta for m in migration] == [-7]


async def test_matrix_put_writes_audit_and_omnibus_rows(
    client, db_session, user_factory, product_factory
):
    admin = user_factory(email="var-audit@example.com", role=UserRole.ADMIN)
    product = product_factory(stock=0)

    resp = await _put_matrix(client, admin, product.id)
    assert resp.status_code == 200
    red = next(v for v in resp.json()["variants"] if v["title"] == "Red / XL")

    event = (
        db_session.query(AdminAuditEvent)
        .filter_by(action="variants_matrix_update", resource_type="product")
        .one()
    )
    assert event.resource_id == product.id
    assert event.actor_id == admin.id
    assert len(event.payload["variants_matrix"]["created_variant_ids"]) == 2

    # OMNIBUS: one per-variant row for the priced currency; inheriting (NULL)
    # currencies record nothing at variant grain.
    rows = db_session.query(PriceHistory).filter_by(variant_id=red["id"]).all()
    assert [(r.currency, r.price) for r in rows] == [("USD", 25.0)]


async def test_matrix_put_calls_meili_sync(client, user_factory, product_factory, monkeypatch):
    admin = user_factory(email="var-meili@example.com", role=UserRole.ADMIN)
    product = product_factory(stock=0)
    synced: list[int] = []
    monkeypatch.setattr(
        "app.api.v1.product_variants.search_index.sync_product",
        lambda db, product_id: synced.append(product_id),
    )

    resp = await _put_matrix(client, admin, product.id)
    assert resp.status_code == 200
    assert synced == [product.id]


async def test_matrix_put_404_unknown_product(client, user_factory):
    admin = user_factory(email="var-404@example.com", role=UserRole.ADMIN)
    resp = await _put_matrix(client, admin, 999_999)
    assert resp.status_code == 404


async def test_matrix_put_422_on_duplicate_combination(client, user_factory, product_factory):
    admin = user_factory(email="var-dup@example.com", role=UserRole.ADMIN)
    product = product_factory(stock=0)
    body = {
        "options": [{"name": "Color", "position": 0, "values": [{"value": "Red"}]}],
        "variants": [{"option_values": ["Red"]}, {"option_values": ["Red"]}],
    }
    resp = await _put_matrix(client, admin, product.id, body)
    assert resp.status_code == 422
    assert "Duplicate variant combination" in resp.json()["detail"]


async def test_matrix_put_422_on_fourth_option(client, user_factory, product_factory):
    admin = user_factory(email="var-cap@example.com", role=UserRole.ADMIN)
    product = product_factory(stock=0)
    body = {
        "options": [
            {"name": f"Option {i}", "position": i, "values": [{"value": "V"}]} for i in range(4)
        ],
        "variants": [],
    }
    resp = await _put_matrix(client, admin, product.id, body)
    assert resp.status_code == 422  # Pydantic max_length on the options list


async def test_matrix_put_400_on_taken_sku(client, user_factory, product_factory):
    admin = user_factory(email="var-sku@example.com", role=UserRole.ADMIN)
    product_a = product_factory(stock=0)
    product_b = product_factory(stock=0)
    assert (await _put_matrix(client, admin, product_a.id)).status_code == 200

    body = {
        "options": [{"name": "Color", "position": 0, "values": [{"value": "Red"}]}],
        "variants": [{"option_values": ["Red"], "sku": "API-RED-XL"}],
    }
    resp = await _put_matrix(client, admin, product_b.id, body)
    assert resp.status_code == 400
    assert "already in use" in resp.json()["detail"]


async def test_matrix_put_empty_reverts_to_single_sku(
    client, db_session, user_factory, product_factory
):
    admin = user_factory(email="var-revert@example.com", role=UserRole.ADMIN)
    product = product_factory(stock=0)
    assert (await _put_matrix(client, admin, product.id)).status_code == 200

    resp = await _put_matrix(client, admin, product.id, {"options": [], "variants": []})
    assert resp.status_code == 200
    assert resp.json() == {"options": [], "variants": []}
    db_session.refresh(product)
    assert product.stock == 0


# ---------------------------------------------------------------------------
# GET variants (admin list)
# ---------------------------------------------------------------------------


async def test_admin_variant_list_includes_inactive(client, user_factory, product_factory):
    admin = user_factory(email="var-list@example.com", role=UserRole.ADMIN)
    product = product_factory(stock=0)
    assert (await _put_matrix(client, admin, product.id)).status_code == 200
    listing = await client.get(f"/api/v1/products/{product.id}/variants", headers=_headers(admin))
    red = next(v for v in listing.json() if v["title"] == "Red / XL")
    deactivate = await client.patch(
        f"/api/v1/products/{product.id}/variants/{red['id']}",
        json={"is_active": False},
        headers=_headers(admin),
    )
    assert deactivate.status_code == 200

    listing = await client.get(f"/api/v1/products/{product.id}/variants", headers=_headers(admin))
    assert listing.status_code == 200
    assert {v["title"]: v["is_active"] for v in listing.json()} == {
        "Red / XL": False,
        "Blue / XL": True,
    }


# ---------------------------------------------------------------------------
# PATCH variant
# ---------------------------------------------------------------------------


async def test_patch_variant_updates_price_and_records_omnibus(
    client, db_session, user_factory, product_factory
):
    admin = user_factory(email="var-patch@example.com", role=UserRole.ADMIN)
    product = product_factory(stock=0)
    resp = await _put_matrix(client, admin, product.id)
    blue = next(v for v in resp.json()["variants"] if v["title"] == "Blue / XL")

    patched = await client.patch(
        f"/api/v1/products/{product.id}/variants/{blue['id']}",
        json={"price_usd": 19.0, "sku": "API-BLUE-XL"},
        headers=_headers(admin),
    )
    assert patched.status_code == 200
    assert patched.json()["price_usd"] == 19.0
    assert patched.json()["sku"] == "API-BLUE-XL"

    rows = db_session.query(PriceHistory).filter_by(variant_id=blue["id"]).all()
    assert [(r.currency, r.price) for r in rows] == [("USD", 19.0)]
    event = (
        db_session.query(AdminAuditEvent)
        .filter_by(action="update", resource_type="product_variant", resource_id=blue["id"])
        .one()
    )
    assert event.payload["after"]["price_usd"] == 19.0


async def test_patch_variant_400_on_taken_sku(client, user_factory, product_factory):
    admin = user_factory(email="var-patch-sku@example.com", role=UserRole.ADMIN)
    product = product_factory(stock=0)
    resp = await _put_matrix(client, admin, product.id)
    blue = next(v for v in resp.json()["variants"] if v["title"] == "Blue / XL")

    patched = await client.patch(
        f"/api/v1/products/{product.id}/variants/{blue['id']}",
        json={"sku": "API-RED-XL"},  # taken by the sibling
        headers=_headers(admin),
    )
    assert patched.status_code == 400


async def test_patch_variant_404s(client, user_factory, product_factory):
    admin = user_factory(email="var-patch-404@example.com", role=UserRole.ADMIN)
    product = product_factory(stock=0)
    other = product_factory(stock=0)
    resp = await _put_matrix(client, admin, product.id)
    red = next(v for v in resp.json()["variants"] if v["title"] == "Red / XL")

    assert (
        await client.patch(
            f"/api/v1/products/{product.id}/variants/999999",
            json={},
            headers=_headers(admin),
        )
    ).status_code == 404
    # A variant fetched through another product 404s (scoped lookup).
    assert (
        await client.patch(
            f"/api/v1/products/{other.id}/variants/{red['id']}",
            json={},
            headers=_headers(admin),
        )
    ).status_code == 404


# ---------------------------------------------------------------------------
# DELETE variant
# ---------------------------------------------------------------------------


async def test_delete_variant_zeroes_stock_and_409s_on_last_active(
    client, db_session, user_factory, product_factory
):
    admin = user_factory(email="var-del@example.com", role=UserRole.ADMIN)
    product = product_factory(stock=0)
    resp = await _put_matrix(client, admin, product.id)
    variants = {v["title"]: v for v in resp.json()["variants"]}

    deleted = await client.delete(
        f"/api/v1/products/{product.id}/variants/{variants['Blue / XL']['id']}",
        headers=_headers(admin),
    )
    assert deleted.status_code == 204
    db_session.refresh(product)
    assert product.stock == 4  # Blue's 6 zeroed through the ledger

    # Red is now the last active variant — deleting it is a 409.
    last = await client.delete(
        f"/api/v1/products/{product.id}/variants/{variants['Red / XL']['id']}",
        headers=_headers(admin),
    )
    assert last.status_code == 409
    assert "last active variant" in last.json()["detail"]


# ---------------------------------------------------------------------------
# Public product detail (additive fields)
# ---------------------------------------------------------------------------


async def test_public_detail_carries_options_variants_and_filters_inactive(
    client, user_factory, product_factory
):
    admin = user_factory(email="var-public@example.com", role=UserRole.ADMIN)
    product = product_factory(stock=0, price_usd=20.0)
    resp = await _put_matrix(client, admin, product.id)
    blue = next(v for v in resp.json()["variants"] if v["title"] == "Blue / XL")
    deactivated = await client.patch(
        f"/api/v1/products/{product.id}/variants/{blue['id']}",
        json={"is_active": False},
        headers=_headers(admin),
    )
    assert deactivated.status_code == 200

    detail = await client.get(f"/api/v1/products/{product.id}")
    assert detail.status_code == 200
    data = detail.json()

    assert data["has_variants"] is True
    assert [option["name"] for option in data["options"]] == ["Color", "Size"]
    # Inactive variants are filtered from the public view.
    assert [variant["title"] for variant in data["variants"]] == ["Red / XL"]
    public_red = data["variants"][0]
    assert public_red["price_usd"] == 25.0  # own override (effective price)
    assert public_red["in_stock"] is True
    assert len(public_red["option_value_ids"]) == 2

    by_slug = await client.get(f"/api/v1/products/slug/{product.slug}")
    assert by_slug.status_code == 200
    assert by_slug.json()["has_variants"] is True


async def test_public_detail_variantless_product_unchanged(client, product_factory):
    product = product_factory(stock=3)

    detail = await client.get(f"/api/v1/products/{product.id}")
    assert detail.status_code == 200
    data = detail.json()
    assert data["has_variants"] is False
    assert data["options"] == []
    assert data["variants"] == []


async def test_public_list_carries_has_variants(client, user_factory, product_factory):
    admin = user_factory(email="var-pub-list@example.com", role=UserRole.ADMIN)
    product_factory(stock=3, name="Plainware")
    varied = product_factory(stock=0, name="Variedware")
    assert (await _put_matrix(client, admin, varied.id)).status_code == 200

    listing = await client.get("/api/v1/products/")
    assert listing.status_code == 200
    flags = {item["name"]: item["has_variants"] for item in listing.json()["items"]}
    assert flags["Plainware"] is False
    assert flags["Variedware"] is True


# ---------------------------------------------------------------------------
# Omnibus lowest-30d variant scoping
# ---------------------------------------------------------------------------


async def test_lowest_30d_variant_scoped(client, user_factory, product_factory):
    admin = user_factory(email="var-omnibus@example.com", role=UserRole.ADMIN)
    product = product_factory(stock=0, price_usd=20.0)
    resp = await _put_matrix(client, admin, product.id)
    red = next(v for v in resp.json()["variants"] if v["title"] == "Red / XL")

    # Cut the variant's USD price — its own trail now holds 25.0 then 19.0.
    patched = await client.patch(
        f"/api/v1/products/{product.id}/variants/{red['id']}",
        json={"price_usd": 19.0},
        headers=_headers(admin),
    )
    assert patched.status_code == 200

    scoped = await client.get(
        f"/api/v1/products/{product.id}/lowest-30d",
        params={"currency": "USD", "variant_id": red["id"]},
    )
    assert scoped.status_code == 200
    assert scoped.json()["lowest_price"] == 19.0

    # Unknown variant 404s; omitted variant_id reads the product-grain trail.
    assert (
        await client.get(
            f"/api/v1/products/{product.id}/lowest-30d",
            params={"currency": "USD", "variant_id": 999_999},
        )
    ).status_code == 404
    product_grain = await client.get(
        f"/api/v1/products/{product.id}/lowest-30d", params={"currency": "USD"}
    )
    assert product_grain.status_code == 200
