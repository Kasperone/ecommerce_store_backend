"""Integration tests for B2B price lists (Shopify-gap P2 #14).

Covers:

- Admin price-list + entry CRUD: auth gating (401 anon / 403 non-admin), create
  list, add entry (+ duplicate 409, foreign-variant 422).
- Checkout pricing (the money path): an order placed by an active company member
  whose company has an active price list is priced from the list; a product with
  no entry falls back to catalog; a guest always gets catalog.
- Storefront display: a company member sees ``b2b_price_*`` on the product
  detail; an anonymous caller sees ``null``.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.crud import price_list as crud_price_list
from app.models.user import UserRole
from app.schemas.price_list import PriceListCreate, PriceListEntryCreate
from tests.factories.company import CompanyFactory

pytestmark = pytest.mark.integration


def _headers(user) -> dict:
    token, _, _ = create_access_token(subject=str(user.id))
    return {"Authorization": f"Bearer {token}"}


def _order_body(product_id: int, *, shipping_method_id: int, quantity: int = 1) -> dict:
    return {
        "items": [{"product_id": product_id, "quantity": quantity}],
        "currency": "USD",
        "shipping_address": "1 Test St",
        "shipping_city": "Testville",
        "shipping_postal_code": "00-001",
        "shipping_country": "PL",
        "shipping_method_id": shipping_method_id,
    }


@pytest.fixture
def company_factory(db_session: Session):
    CompanyFactory._meta.sqlalchemy_session = db_session  # pyright: ignore[reportAttributeAccessIssue]
    CompanyFactory._meta.sqlalchemy_session_persistence = "commit"  # pyright: ignore[reportAttributeAccessIssue]

    def _make(**kwargs):
        return CompanyFactory(**kwargs)

    return _make


def _make_list_with_entry(db: Session, *, product_id: int, price_usd: float, active: bool = True):
    """Create an active price list with a single product-grain USD entry."""
    pl = crud_price_list.create(db, obj_in=PriceListCreate(name="B2B Wholesale", is_active=active))
    crud_price_list.add_entry(
        db,
        price_list_id=pl.id,
        obj_in=PriceListEntryCreate(product_id=product_id, price_usd=price_usd),
    )
    db.commit()
    return pl


# ---------------------------------------------------------------------------
# Admin CRUD auth + happy path
# ---------------------------------------------------------------------------


async def test_admin_routes_require_admin(client, user_factory):
    customer = user_factory(email="pl-cust@example.com")
    assert (await client.get("/api/v1/admin/price-lists/")).status_code == 401
    assert (
        await client.get("/api/v1/admin/price-lists/", headers=_headers(customer))
    ).status_code == 403


async def test_admin_create_and_add_entry(client, user_factory, product_factory):
    admin = user_factory(email="pl-admin@example.com", role=UserRole.ADMIN)
    product = product_factory(price_usd=19.99)

    created = await client.post(
        "/api/v1/admin/price-lists/",
        json={"name": "Gold Tier", "notes": "top accounts"},
        headers=_headers(admin),
    )
    assert created.status_code == 201, created.text
    pl_id = created.json()["id"]
    assert created.json()["is_active"] is True
    assert created.json()["entries"] == []

    entry = await client.post(
        f"/api/v1/admin/price-lists/{pl_id}/entries",
        json={"product_id": product.id, "price_usd": 12.5},
        headers=_headers(admin),
    )
    assert entry.status_code == 201, entry.text
    assert entry.json()["price_usd"] == 12.5
    assert entry.json()["product_name"] == product.name

    # Duplicate grain → 409.
    dup = await client.post(
        f"/api/v1/admin/price-lists/{pl_id}/entries",
        json={"product_id": product.id, "price_usd": 9.0},
        headers=_headers(admin),
    )
    assert dup.status_code == 409

    # No prices at all → 422 (schema at-least-one-price).
    empty = await client.post(
        f"/api/v1/admin/price-lists/{pl_id}/entries",
        json={"product_id": product.id},
        headers=_headers(admin),
    )
    assert empty.status_code == 422


# ---------------------------------------------------------------------------
# Checkout pricing (money path)
# ---------------------------------------------------------------------------


async def test_checkout_prices_from_list(
    client, user_factory, product_factory, company_factory, db_session, test_shipping_method_id
):
    buyer = user_factory(email="pl-buyer@example.com")
    product = product_factory(stock=10, price_usd=19.99)
    pl = _make_list_with_entry(db_session, product_id=product.id, price_usd=5.00)
    # Primary contact resolves as an active COMPANY_ADMIN membership.
    company_factory(name="Acme", primary_user_id=buyer.id, price_list_id=pl.id)

    resp = await client.post(
        "/api/v1/orders/",
        json=_order_body(product.id, quantity=2, shipping_method_id=test_shipping_method_id),
        headers=_headers(buyer),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    # Priced from the list ($5), not the catalog ($19.99).
    assert body["items"][0]["unit_price"] == 5.00
    assert body["subtotal"] == 10.00


async def test_checkout_product_without_entry_uses_catalog(
    client, user_factory, product_factory, company_factory, db_session, test_shipping_method_id
):
    buyer = user_factory(email="pl-buyer2@example.com")
    listed = product_factory(stock=10, price_usd=19.99)
    unlisted = product_factory(stock=10, price_usd=30.00)
    pl = _make_list_with_entry(db_session, product_id=listed.id, price_usd=5.00)
    company_factory(name="Acme2", primary_user_id=buyer.id, price_list_id=pl.id)

    resp = await client.post(
        "/api/v1/orders/",
        json=_order_body(unlisted.id, quantity=1, shipping_method_id=test_shipping_method_id),
        headers=_headers(buyer),
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["items"][0]["unit_price"] == 30.00


async def test_guest_checkout_uses_catalog(
    client, product_factory, db_session, test_shipping_method_id
):
    product = product_factory(stock=10, price_usd=19.99)
    _make_list_with_entry(db_session, product_id=product.id, price_usd=5.00)

    body = _order_body(product.id, quantity=1, shipping_method_id=test_shipping_method_id)
    body["guest_email"] = "guest-pl@example.com"
    resp = await client.post("/api/v1/orders/", json=body)
    assert resp.status_code == 201, resp.text
    # No company → catalog price, even though a list has an entry for this product.
    assert resp.json()["items"][0]["unit_price"] == 19.99


async def test_inactive_list_uses_catalog(
    client, user_factory, product_factory, company_factory, db_session, test_shipping_method_id
):
    buyer = user_factory(email="pl-buyer3@example.com")
    product = product_factory(stock=10, price_usd=19.99)
    pl = _make_list_with_entry(db_session, product_id=product.id, price_usd=5.00, active=False)
    company_factory(name="Acme3", primary_user_id=buyer.id, price_list_id=pl.id)

    resp = await client.post(
        "/api/v1/orders/",
        json=_order_body(product.id, quantity=1, shipping_method_id=test_shipping_method_id),
        headers=_headers(buyer),
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["items"][0]["unit_price"] == 19.99


# ---------------------------------------------------------------------------
# Storefront display
# ---------------------------------------------------------------------------


async def test_storefront_detail_shows_b2b_price(
    client, user_factory, product_factory, company_factory, db_session
):
    buyer = user_factory(email="pl-view@example.com")
    product = product_factory(price_usd=19.99, slug="widget-b2b")
    pl = _make_list_with_entry(db_session, product_id=product.id, price_usd=5.00)
    company_factory(name="Acme4", primary_user_id=buyer.id, price_list_id=pl.id)

    as_member = await client.get("/api/v1/products/slug/widget-b2b", headers=_headers(buyer))
    assert as_member.status_code == 200
    assert as_member.json()["b2b_price_usd"] == 5.00

    anon = await client.get("/api/v1/products/slug/widget-b2b")
    assert anon.status_code == 200
    assert anon.json()["b2b_price_usd"] is None
