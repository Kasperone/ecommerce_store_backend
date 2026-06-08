"""Integration tests for the admin product bulk-edit (CSV-BE3).

Covers ``PATCH /api/v1/admin/products/bulk`` — auth gating, the narrow settable
set (price / is_active / low_stock_threshold), OMNIBUS price-history recorded per
changed product on a price change, all-or-nothing 404 on an unknown id (no partial
writes), the validation matrix (empty product_ids / empty set / negative price /
``stock`` not settable — the schema forbids it), and that a successful bulk edit
fires the incremental Meili sync.
"""

from __future__ import annotations

import pytest

from app.core.security import create_access_token
from app.models.price_history import PriceHistory
from app.models.product import Product
from app.models.product_variant import ProductVariant
from app.models.user import UserRole

pytestmark = pytest.mark.integration

BULK_URL = "/api/v1/admin/products/bulk"


def _headers(user) -> dict:
    token, _, _ = create_access_token(subject=str(user.id))
    return {"Authorization": f"Bearer {token}"}


# --------------------------------------------------------------------------- #
# Auth gating
# --------------------------------------------------------------------------- #


async def test_bulk_requires_auth(client):
    resp = await client.patch(BULK_URL, json={"product_ids": [1], "set": {"is_active": False}})
    assert resp.status_code == 401


async def test_bulk_forbidden_for_customer(client, user_factory, product_factory):
    customer = user_factory(email="bulk-cust@example.com")
    product = product_factory(name="X", slug="bulk-cust-p", price_usd=10.0)
    resp = await client.patch(
        BULK_URL,
        json={"product_ids": [product.id], "set": {"is_active": False}},
        headers=_headers(customer),
    )
    assert resp.status_code == 403


# --------------------------------------------------------------------------- #
# Happy paths
# --------------------------------------------------------------------------- #


async def test_bulk_price_update_records_price_history(
    client, user_factory, product_factory, db_session
):
    admin = user_factory(email="bulk-price@example.com", role=UserRole.ADMIN)
    p1 = product_factory(name="P1", slug="bulk-p1", price_usd=10.0, price_pln=40.0, price_eur=9.0)
    p2 = product_factory(name="P2", slug="bulk-p2", price_usd=20.0, price_pln=80.0, price_eur=18.0)

    resp = await client.patch(
        BULK_URL,
        json={"product_ids": [p1.id, p2.id], "set": {"price_usd": 12.5}},
        headers=_headers(admin),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["updated"] == 2
    assert set(body["product_ids"]) == {p1.id, p2.id}

    db_session.expire_all()
    assert db_session.get(Product, p1.id).price_usd == 12.5
    assert db_session.get(Product, p2.id).price_usd == 12.5
    # PLN/EUR untouched.
    assert db_session.get(Product, p1.id).price_pln == 40.0

    # OMNIBUS: a USD price-history row recorded for each changed product.
    for pid in (p1.id, p2.id):
        rows = (
            db_session.query(PriceHistory)
            .filter(PriceHistory.product_id == pid, PriceHistory.currency == "USD")
            .all()
        )
        assert any(r.price == 12.5 for r in rows)


async def test_bulk_set_is_active_and_threshold(client, user_factory, product_factory, db_session):
    admin = user_factory(email="bulk-flags@example.com", role=UserRole.ADMIN)
    p1 = product_factory(name="A", slug="bulk-a", price_usd=10.0, is_active=True)
    p2 = product_factory(name="B", slug="bulk-b", price_usd=10.0, is_active=True)

    resp = await client.patch(
        BULK_URL,
        json={
            "product_ids": [p1.id, p2.id],
            "set": {"is_active": False, "low_stock_threshold": 5},
        },
        headers=_headers(admin),
    )
    assert resp.status_code == 200
    assert resp.json()["updated"] == 2

    db_session.expire_all()
    for pid in (p1.id, p2.id):
        prod = db_session.get(Product, pid)
        assert prod.is_active is False
        assert prod.low_stock_threshold == 5


async def test_bulk_threshold_null_clears(client, user_factory, product_factory, db_session):
    admin = user_factory(email="bulk-null@example.com", role=UserRole.ADMIN)
    p1 = product_factory(name="C", slug="bulk-c", price_usd=10.0, low_stock_threshold=3)

    resp = await client.patch(
        BULK_URL,
        json={"product_ids": [p1.id], "set": {"low_stock_threshold": None}},
        headers=_headers(admin),
    )
    assert resp.status_code == 200
    db_session.expire_all()
    assert db_session.get(Product, p1.id).low_stock_threshold is None


async def test_bulk_no_price_change_records_no_history(
    client, user_factory, product_factory, db_session
):
    admin = user_factory(email="bulk-noprice@example.com", role=UserRole.ADMIN)
    p1 = product_factory(name="D", slug="bulk-d", price_usd=10.0)
    before = db_session.query(PriceHistory).filter(PriceHistory.product_id == p1.id).count()

    resp = await client.patch(
        BULK_URL,
        json={"product_ids": [p1.id], "set": {"is_active": False}},
        headers=_headers(admin),
    )
    assert resp.status_code == 200
    after = db_session.query(PriceHistory).filter(PriceHistory.product_id == p1.id).count()
    assert after == before


# --------------------------------------------------------------------------- #
# Stock is NOT settable
# --------------------------------------------------------------------------- #


async def test_bulk_stock_is_not_settable(client, user_factory, product_factory):
    admin = user_factory(email="bulk-stock@example.com", role=UserRole.ADMIN)
    product = product_factory(name="Stocky", slug="bulk-stocky", price_usd=10.0, stock=5)
    # The bulk ``set`` schema is ``extra="forbid"`` — a ``stock`` key is rejected.
    resp = await client.patch(
        BULK_URL,
        json={"product_ids": [product.id], "set": {"stock": 99}},
        headers=_headers(admin),
    )
    assert resp.status_code == 422


def test_bulk_set_schema_forbids_stock():
    """The schema itself forbids ``stock`` — stock never flows through bulk-edit."""
    from pydantic import ValidationError

    from app.schemas.product import ProductBulkSet

    with pytest.raises(ValidationError):
        ProductBulkSet.model_validate({"stock": 10})
    assert "stock" not in ProductBulkSet.model_fields


# --------------------------------------------------------------------------- #
# Validation matrix
# --------------------------------------------------------------------------- #


async def test_bulk_empty_product_ids_422(client, user_factory):
    admin = user_factory(email="bulk-empty-ids@example.com", role=UserRole.ADMIN)
    resp = await client.patch(
        BULK_URL,
        json={"product_ids": [], "set": {"is_active": False}},
        headers=_headers(admin),
    )
    assert resp.status_code == 422


async def test_bulk_empty_set_422(client, user_factory, product_factory):
    admin = user_factory(email="bulk-empty-set@example.com", role=UserRole.ADMIN)
    product = product_factory(name="E", slug="bulk-e", price_usd=10.0)
    resp = await client.patch(
        BULK_URL,
        json={"product_ids": [product.id], "set": {}},
        headers=_headers(admin),
    )
    assert resp.status_code == 422


async def test_bulk_negative_price_422(client, user_factory, product_factory):
    admin = user_factory(email="bulk-neg@example.com", role=UserRole.ADMIN)
    product = product_factory(name="F", slug="bulk-f", price_usd=10.0)
    resp = await client.patch(
        BULK_URL,
        json={"product_ids": [product.id], "set": {"price_usd": -1}},
        headers=_headers(admin),
    )
    assert resp.status_code == 422


async def test_bulk_zero_price_422(client, user_factory, product_factory):
    admin = user_factory(email="bulk-zero@example.com", role=UserRole.ADMIN)
    product = product_factory(name="G", slug="bulk-g", price_usd=10.0)
    resp = await client.patch(
        BULK_URL,
        json={"product_ids": [product.id], "set": {"price_usd": 0}},
        headers=_headers(admin),
    )
    assert resp.status_code == 422


async def test_bulk_negative_threshold_422(client, user_factory, product_factory):
    admin = user_factory(email="bulk-negthr@example.com", role=UserRole.ADMIN)
    product = product_factory(name="H", slug="bulk-h", price_usd=10.0)
    resp = await client.patch(
        BULK_URL,
        json={"product_ids": [product.id], "set": {"low_stock_threshold": -1}},
        headers=_headers(admin),
    )
    assert resp.status_code == 422


async def test_bulk_duplicate_ids_422(client, user_factory, product_factory):
    admin = user_factory(email="bulk-dup@example.com", role=UserRole.ADMIN)
    product = product_factory(name="I", slug="bulk-i", price_usd=10.0)
    resp = await client.patch(
        BULK_URL,
        json={"product_ids": [product.id, product.id], "set": {"is_active": False}},
        headers=_headers(admin),
    )
    assert resp.status_code == 422


# --------------------------------------------------------------------------- #
# Unknown id → all-or-nothing 404 (no partial writes)
# --------------------------------------------------------------------------- #


async def test_bulk_unknown_id_404_no_partial_writes(
    client, user_factory, product_factory, db_session
):
    admin = user_factory(email="bulk-unknown@example.com", role=UserRole.ADMIN)
    good = product_factory(name="Good", slug="bulk-good", price_usd=10.0, is_active=True)

    resp = await client.patch(
        BULK_URL,
        json={"product_ids": [good.id, 999999], "set": {"is_active": False}},
        headers=_headers(admin),
    )
    assert resp.status_code == 404
    assert "999999" in resp.json()["detail"]

    # All-or-nothing: the good product must be UNCHANGED.
    db_session.expire_all()
    assert db_session.get(Product, good.id).is_active is True


# --------------------------------------------------------------------------- #
# Variant products — bulk-edit of price/active/threshold is allowed (no stock)
# --------------------------------------------------------------------------- #


async def test_bulk_edit_variant_product_price_allowed(
    client, user_factory, product_factory, db_session
):
    """Bulk-edit touches none of variant stock, so a variant product's price can
    still be bulk-set (stock is simply not a settable field for anyone)."""
    admin = user_factory(email="bulk-var@example.com", role=UserRole.ADMIN)
    product = product_factory(name="VarP", slug="bulk-var", price_usd=10.0)
    db_session.add(
        ProductVariant(
            product_id=product.id,
            title="Red",
            sku="BULK-VAR-RED",
            stock=5,
            is_active=True,
            option_signature="red",
        )
    )
    db_session.commit()

    resp = await client.patch(
        BULK_URL,
        json={"product_ids": [product.id], "set": {"price_usd": 15.0}},
        headers=_headers(admin),
    )
    assert resp.status_code == 200
    db_session.expire_all()
    assert db_session.get(Product, product.id).price_usd == 15.0


# --------------------------------------------------------------------------- #
# Meili sync
# --------------------------------------------------------------------------- #


async def test_bulk_triggers_meili_sync(client, user_factory, product_factory, monkeypatch):
    admin = user_factory(email="bulk-meili@example.com", role=UserRole.ADMIN)
    p1 = product_factory(name="M1", slug="bulk-m1", price_usd=10.0)
    p2 = product_factory(name="M2", slug="bulk-m2", price_usd=10.0)
    calls: list[list[int]] = []

    import app.api.v1.admin_products as mod

    monkeypatch.setattr(
        mod.search_index, "sync_products_bulk", lambda db, ids: calls.append(list(ids))
    )

    resp = await client.patch(
        BULK_URL,
        json={"product_ids": [p1.id, p2.id], "set": {"is_active": True}},
        headers=_headers(admin),
    )
    assert resp.status_code == 200
    assert len(calls) == 1
    assert set(calls[0]) == {p1.id, p2.id}
