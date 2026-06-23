"""Integration tests for the admin product CSV import (CSV-BE2).

Covers ``POST /api/v1/admin/products/import`` — auth gating, dry-run returns a
correct per-row report + writes NOTHING, apply create/update happy paths (tags
get-or-create, collection replace, category resolve, OMNIBUS price-history on a
price change), variant-product stock-change rejection, the row-error matrix
(unknown id / unknown category_slug / new product missing price), all-or-nothing
rollback, slug/SKU uniqueness surfaced as a row error (not a 500), and that a
successful apply triggers the Meili bulk sync.
"""

from __future__ import annotations

import csv
import io
from decimal import Decimal

import pytest

from app.core.security import create_access_token
from app.models.collection import Collection
from app.models.price_history import PriceHistory
from app.models.product import Product
from app.models.product_tag import ProductTag
from app.models.user import UserRole
from app.services.product_export import CSV_HEADERS

pytestmark = pytest.mark.integration

IMPORT_URL = "/api/v1/admin/products/import"


def _headers(user) -> dict:
    token, _, _ = create_access_token(subject=str(user.id))
    return {"Authorization": f"Bearer {token}"}


def _csv_bytes(rows: list[dict[str, str]]) -> bytes:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=CSV_HEADERS)
    writer.writeheader()
    for row in rows:
        writer.writerow({h: row.get(h, "") for h in CSV_HEADERS})
    return buf.getvalue().encode("utf-8-sig")


def _upload(rows: list[dict[str, str]]):
    return {"file": ("products.csv", _csv_bytes(rows), "text/csv")}


# --------------------------------------------------------------------------- #
# Auth gating
# --------------------------------------------------------------------------- #


async def test_import_requires_auth(client):
    resp = await client.post(IMPORT_URL, files=_upload([{"slug": "x", "name": "X"}]))
    assert resp.status_code == 401


async def test_import_forbidden_for_customer(client, user_factory):
    customer = user_factory(email="imp-cust@example.com")
    resp = await client.post(
        IMPORT_URL, files=_upload([{"slug": "x", "name": "X"}]), headers=_headers(customer)
    )
    assert resp.status_code == 403


# --------------------------------------------------------------------------- #
# Dry-run
# --------------------------------------------------------------------------- #


async def test_dry_run_reports_and_writes_nothing(
    client, user_factory, product_factory, db_session
):
    admin = user_factory(email="imp-dry@example.com", role=UserRole.ADMIN)
    existing = product_factory(name="Existing", slug="imp-existing", price_usd=10.0)
    before_count = db_session.query(Product).count()
    before_name = existing.name

    rows = [
        {"id": str(existing.id), "name": "Renamed", "price_usd": "12.50"},
        {"slug": "imp-brand-new", "name": "Brand New", "price_usd": "9.99"},
    ]
    resp = await client.post(
        IMPORT_URL, params={"dry_run": "true"}, files=_upload(rows), headers=_headers(admin)
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["dry_run"] is True
    assert body["applied"] is False
    assert body["summary"] == {"creates": 1, "updates": 1, "skips": 0, "errors": 0}

    report = {r["row_number"]: r for r in body["rows"]}
    assert report[1]["action"] == "update"
    assert report[1]["product_id"] == existing.id
    assert report[2]["action"] == "create"
    assert report[2]["product_id"] is None

    # NOTHING was written.
    db_session.expire_all()
    assert db_session.query(Product).count() == before_count
    assert db_session.get(Product, existing.id).name == before_name
    assert db_session.query(Product).filter_by(slug="imp-brand-new").first() is None


# --------------------------------------------------------------------------- #
# Apply — happy paths
# --------------------------------------------------------------------------- #


async def test_apply_creates_new_product(client, user_factory, db_session):
    admin = user_factory(email="imp-create@example.com", role=UserRole.ADMIN)
    rows = [
        {
            "slug": "imp-created",
            "name": "Imported Widget",
            "name_pl": "Widżet",
            "price_usd": "19.99",
            "price_pln": "79.00",
            "is_active": "true",
        }
    ]
    resp = await client.post(
        IMPORT_URL, params={"dry_run": "false"}, files=_upload(rows), headers=_headers(admin)
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["applied"] is True
    assert body["summary"]["creates"] == 1

    created = db_session.query(Product).filter_by(slug="imp-created").one()
    assert created.name == "Imported Widget"
    assert created.name_pl == "Widżet"
    assert created.price_usd == Decimal("19.99")
    # OMNIBUS: a price-history row exists for the created product.
    assert db_session.query(PriceHistory).filter_by(product_id=created.id).count() >= 1


async def test_apply_updates_with_price_history(client, user_factory, product_factory, db_session):
    admin = user_factory(email="imp-update@example.com", role=UserRole.ADMIN)
    product = product_factory(name="Old", slug="imp-upd", price_usd=10.0)
    base_history = db_session.query(PriceHistory).filter_by(product_id=product.id).count()

    rows = [{"id": str(product.id), "name": "New Name", "price_usd": "25.00"}]
    resp = await client.post(
        IMPORT_URL, params={"dry_run": "false"}, files=_upload(rows), headers=_headers(admin)
    )
    assert resp.status_code == 200
    assert resp.json()["summary"]["updates"] == 1

    db_session.expire_all()
    updated = db_session.get(Product, product.id)
    assert updated.name == "New Name"
    assert updated.price_usd == 25.0
    # A new OMNIBUS row was appended for the changed USD price.
    assert db_session.query(PriceHistory).filter_by(product_id=product.id).count() > base_history


async def test_apply_replaces_tags_and_collections(
    client, user_factory, product_factory, db_session
):
    admin = user_factory(email="imp-tags@example.com", role=UserRole.ADMIN)
    product = product_factory(name="Tagme", slug="imp-tags")
    product.tags.append(ProductTag(name="old-tag"))
    summer = Collection(name="Summer", slug="imp-summer")
    winter = Collection(name="Winter", slug="imp-winter")
    db_session.add_all([summer, winter])
    db_session.commit()

    rows = [
        {
            "id": str(product.id),
            "name": "Tagme",
            "tags": "sale; featured",
            "collection_slugs": "imp-summer; imp-winter",
        }
    ]
    resp = await client.post(
        IMPORT_URL, params={"dry_run": "false"}, files=_upload(rows), headers=_headers(admin)
    )
    assert resp.status_code == 200
    assert resp.json()["applied"] is True

    db_session.expire_all()
    refreshed = db_session.get(Product, product.id)
    assert sorted(t.name for t in refreshed.tags) == ["featured", "sale"]
    assert sorted(c.slug for c in refreshed.collections) == ["imp-summer", "imp-winter"]


async def test_apply_resolves_category_slug(
    client, user_factory, product_factory, category_factory, db_session
):
    admin = user_factory(email="imp-cat@example.com", role=UserRole.ADMIN)
    product = product_factory(name="Catme", slug="imp-cat")
    target_cat = category_factory(name="Gadgets", slug="imp-gadgets")

    rows = [{"id": str(product.id), "name": "Catme", "category_slug": "imp-gadgets"}]
    resp = await client.post(
        IMPORT_URL, params={"dry_run": "false"}, files=_upload(rows), headers=_headers(admin)
    )
    assert resp.status_code == 200
    db_session.expire_all()
    assert db_session.get(Product, product.id).category_id == target_cat.id


async def test_apply_changes_stock_via_ledger(client, user_factory, product_factory, db_session):
    admin = user_factory(email="imp-stock@example.com", role=UserRole.ADMIN)
    product = product_factory(name="Stocky", slug="imp-stock", stock=10)

    rows = [{"id": str(product.id), "name": "Stocky", "stock": "42"}]
    resp = await client.post(
        IMPORT_URL, params={"dry_run": "false"}, files=_upload(rows), headers=_headers(admin)
    )
    assert resp.status_code == 200
    assert resp.json()["applied"] is True
    db_session.expire_all()
    assert db_session.get(Product, product.id).stock == 42
    # The change went through the ledger (a movement row exists).
    from app.models.inventory_movement import InventoryMovement

    assert (
        db_session.query(InventoryMovement)
        .filter_by(product_id=product.id, reason="manual_adjustment")
        .count()
        >= 1
    )


# --------------------------------------------------------------------------- #
# Variant-stock rejection
# --------------------------------------------------------------------------- #


async def test_variant_product_stock_change_rejected(
    client, user_factory, product_factory, db_session
):
    admin = user_factory(email="imp-variant@example.com", role=UserRole.ADMIN)
    product = product_factory(name="Varianty", slug="imp-variant", stock=5)
    # Attach a variant so the product is variant-grain.
    from app.models.product_variant import ProductVariant

    variant = ProductVariant(
        product_id=product.id,
        title="Red",
        sku="IMP-VAR-RED",
        stock=5,
        is_active=True,
        option_signature="red",
    )
    db_session.add(variant)
    db_session.commit()

    rows = [{"id": str(product.id), "name": "Varianty", "stock": "99"}]
    resp = await client.post(
        IMPORT_URL, params={"dry_run": "true"}, files=_upload(rows), headers=_headers(admin)
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["summary"]["errors"] == 1
    assert any("variant grain" in e for e in body["rows"][0]["errors"])


# --------------------------------------------------------------------------- #
# Row-error matrix
# --------------------------------------------------------------------------- #


async def test_unknown_id_is_row_error(client, user_factory):
    admin = user_factory(email="imp-badid@example.com", role=UserRole.ADMIN)
    rows = [{"id": "999999", "name": "Ghost"}]
    resp = await client.post(
        IMPORT_URL, params={"dry_run": "true"}, files=_upload(rows), headers=_headers(admin)
    )
    body = resp.json()
    assert body["summary"]["errors"] == 1
    assert any("No product with id" in e for e in body["rows"][0]["errors"])


async def test_unknown_category_slug_is_row_error(client, user_factory, product_factory):
    admin = user_factory(email="imp-badcat@example.com", role=UserRole.ADMIN)
    product = product_factory(name="X", slug="imp-badcat")
    rows = [{"id": str(product.id), "name": "X", "category_slug": "does-not-exist"}]
    resp = await client.post(
        IMPORT_URL, params={"dry_run": "true"}, files=_upload(rows), headers=_headers(admin)
    )
    body = resp.json()
    assert body["summary"]["errors"] == 1
    assert any("Unknown category_slug" in e for e in body["rows"][0]["errors"])


async def test_new_product_missing_price_is_row_error(client, user_factory):
    admin = user_factory(email="imp-noprice@example.com", role=UserRole.ADMIN)
    rows = [{"slug": "imp-noprice", "name": "No Price"}]
    resp = await client.post(
        IMPORT_URL, params={"dry_run": "true"}, files=_upload(rows), headers=_headers(admin)
    )
    body = resp.json()
    assert body["summary"]["errors"] == 1
    assert any("at least one price" in e for e in body["rows"][0]["errors"])


# --------------------------------------------------------------------------- #
# All-or-nothing + uniqueness
# --------------------------------------------------------------------------- #


async def test_apply_all_or_nothing_rollback(client, user_factory, product_factory, db_session):
    admin = user_factory(email="imp-atomic@example.com", role=UserRole.ADMIN)
    good_target = product_factory(name="Good", slug="imp-good", price_usd=10.0)
    before_count = db_session.query(Product).count()

    rows = [
        {"id": str(good_target.id), "name": "Good Renamed", "price_usd": "11.00"},
        {"slug": "imp-would-create", "name": "Would Create", "price_usd": "5.00"},
        {"id": "888888", "name": "Bad"},  # unknown id → aborts the whole batch
    ]
    resp = await client.post(
        IMPORT_URL, params={"dry_run": "false"}, files=_upload(rows), headers=_headers(admin)
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["applied"] is False
    assert body["summary"]["errors"] == 1

    # Nothing applied — the good rows rolled back too.
    db_session.expire_all()
    assert db_session.query(Product).count() == before_count
    assert db_session.get(Product, good_target.id).name == "Good"
    assert db_session.query(Product).filter_by(slug="imp-would-create").first() is None


async def test_slug_collision_is_row_error_not_500(
    client, user_factory, product_factory, db_session
):
    admin = user_factory(email="imp-slugdupe@example.com", role=UserRole.ADMIN)
    # Two existing products. Updating A (by id) to take B's slug must surface the
    # route-layer uniqueness check as a row error (a clean report), never a 500.
    product_a = product_factory(name="Alpha", slug="imp-alpha", price_usd=10.0)
    product_b = product_factory(name="Beta", slug="imp-beta", price_usd=10.0)
    rows = [
        {"id": str(product_a.id), "name": "Alpha", "slug": "imp-beta"},  # B's slug
    ]
    resp = await client.post(
        IMPORT_URL, params={"dry_run": "false"}, files=_upload(rows), headers=_headers(admin)
    )
    assert resp.status_code == 200  # not a 500
    body = resp.json()
    assert body["applied"] is False
    assert body["summary"]["errors"] == 1
    assert any("slug" in e.lower() and "exists" in e.lower() for e in body["rows"][0]["errors"])
    # Nothing changed — A keeps its slug, B keeps its slug.
    db_session.expire_all()
    assert db_session.get(Product, product_a.id).slug == "imp-alpha"
    assert db_session.get(Product, product_b.id).slug == "imp-beta"


# --------------------------------------------------------------------------- #
# Meili sync on apply
# --------------------------------------------------------------------------- #


async def test_apply_triggers_meili_bulk_sync(client, user_factory, monkeypatch):
    admin = user_factory(email="imp-meili@example.com", role=UserRole.ADMIN)
    calls: list[list[int]] = []

    import app.api.v1.admin_products as mod

    def _spy(db, product_ids):
        calls.append(list(product_ids))

    monkeypatch.setattr(mod.search_index, "sync_products_bulk", _spy)

    rows = [{"slug": "imp-meili", "name": "Meili", "price_usd": "7.00"}]
    resp = await client.post(
        IMPORT_URL, params={"dry_run": "false"}, files=_upload(rows), headers=_headers(admin)
    )
    assert resp.status_code == 200
    assert resp.json()["applied"] is True
    assert len(calls) == 1
    assert len(calls[0]) == 1  # one touched product id


async def test_dry_run_does_not_trigger_meili(client, user_factory, monkeypatch):
    admin = user_factory(email="imp-nomeili@example.com", role=UserRole.ADMIN)
    calls: list[list[int]] = []
    import app.api.v1.admin_products as mod

    monkeypatch.setattr(
        mod.search_index, "sync_products_bulk", lambda db, ids: calls.append(list(ids))
    )

    rows = [{"slug": "imp-nomeili", "name": "X", "price_usd": "7.00"}]
    resp = await client.post(
        IMPORT_URL, params={"dry_run": "true"}, files=_upload(rows), headers=_headers(admin)
    )
    assert resp.status_code == 200
    assert calls == []
