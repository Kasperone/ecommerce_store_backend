"""Integration tests for the related-products endpoint.

``GET /api/v1/products/{slug}/related`` returns a ranked list of
``ProductResponse`` cards for the PDP ("customers also bought"):

- Primary signal: co-occurrence — other products that share a non-cancelled
  order with the subject, ranked by distinct-order count (desc).
- Fallback fill: active, in-stock products from the same category (newest
  first) when co-occurrence is short of ``limit``.
- Subject excluded, inactive excluded, de-duped, capped at ``limit``.
"""

from __future__ import annotations

import itertools

import pytest

from app.models.order import Order, OrderItem, OrderStatus

pytestmark = pytest.mark.integration

_order_seq = itertools.count(1)


def _make_order(db, *, product_ids: list[int], status: OrderStatus = OrderStatus.PAID) -> Order:
    """Insert an order with one line item per product id."""
    order = Order(
        order_number=f"ORD-REL-{next(_order_seq):05d}",
        guest_email="buyer@example.com",
        status=status,
        subtotal=100.0,
        total=100.0,
        currency="USD",
        shipping_address="1 Main St",
        shipping_city="Warsaw",
        shipping_postal_code="00-001",
        shipping_country="PL",
    )
    db.add(order)
    db.flush()
    for pid in product_ids:
        db.add(
            OrderItem(
                order_id=order.id,
                product_id=pid,
                product_name="Item",
                quantity=1,
                unit_price=50.0,
                total_price=50.0,
            )
        )
    db.commit()
    return order


# ---------------------------------------------------------------------------
# Co-occurrence ranking (the primary "customers also bought" signal)
# ---------------------------------------------------------------------------


async def test_cooccurrence_ranks_by_distinct_order_count(client, db_session, product_factory):
    """B co-occurs with A in two orders, C in one → B ranks before C."""
    a = product_factory(slug="rel-a")
    b = product_factory(slug="rel-b")
    c = product_factory(slug="rel-c")

    _make_order(db_session, product_ids=[a.id, b.id])
    _make_order(db_session, product_ids=[a.id, b.id])
    _make_order(db_session, product_ids=[a.id, c.id])

    response = await client.get(f"/api/v1/products/{a.slug}/related")

    assert response.status_code == 200
    ids = [item["id"] for item in response.json()]
    assert ids == [b.id, c.id]
    # Cards carry the list-item shape (has_variants + in_stock computed field).
    first = response.json()[0]
    assert "has_variants" in first
    assert "in_stock" in first
    assert "images" in first


async def test_subject_product_is_excluded(client, db_session, product_factory):
    a = product_factory(slug="rel-self-a")
    b = product_factory(slug="rel-self-b")
    _make_order(db_session, product_ids=[a.id, b.id])

    response = await client.get(f"/api/v1/products/{a.slug}/related")

    assert response.status_code == 200
    ids = [item["id"] for item in response.json()]
    assert a.id not in ids


async def test_inactive_cooccurring_product_excluded(client, db_session, product_factory):
    a = product_factory(slug="rel-inactive-a")
    active = product_factory(slug="rel-inactive-active")
    inactive = product_factory(slug="rel-inactive-dead", is_active=False)
    _make_order(db_session, product_ids=[a.id, active.id, inactive.id])

    response = await client.get(f"/api/v1/products/{a.slug}/related")

    assert response.status_code == 200
    ids = [item["id"] for item in response.json()]
    assert active.id in ids
    assert inactive.id not in ids


async def test_cancelled_orders_do_not_count(client, db_session, product_factory):
    """A cancelled order is not a 'customers also bought' signal."""
    # A and B sit in distinct categories (factory default), so there is no
    # category fallback to mask the cancelled-order exclusion.
    a = product_factory(slug="rel-cancel-a")
    b = product_factory(slug="rel-cancel-b")
    _make_order(db_session, product_ids=[a.id, b.id], status=OrderStatus.CANCELLED)

    response = await client.get(f"/api/v1/products/{a.slug}/related")

    assert response.status_code == 200
    assert response.json() == []


# ---------------------------------------------------------------------------
# Category fallback fill
# ---------------------------------------------------------------------------


async def test_category_fallback_fills_when_no_cooccurrence(
    client, db_session, product_factory, category_factory
):
    """No orders at all → fill from same-category active, in-stock peers, newest first."""
    cat = category_factory(slug="rel-fallback-cat")
    subject = product_factory(slug="rel-fb-subject", category=cat)
    peer_old = product_factory(slug="rel-fb-old", category=cat)
    peer_new = product_factory(slug="rel-fb-new", category=cat)

    response = await client.get(f"/api/v1/products/{subject.slug}/related")

    assert response.status_code == 200
    ids = [item["id"] for item in response.json()]
    # Newest first; subject excluded.
    assert ids == [peer_new.id, peer_old.id]
    assert subject.id not in ids


async def test_category_fallback_excludes_inactive_and_out_of_stock(
    client, db_session, product_factory, category_factory
):
    cat = category_factory(slug="rel-fb-filter-cat")
    subject = product_factory(slug="rel-fbf-subject", category=cat)
    good = product_factory(slug="rel-fbf-good", category=cat, stock=5)
    dead = product_factory(slug="rel-fbf-dead", category=cat, is_active=False)
    empty = product_factory(slug="rel-fbf-empty", category=cat, stock=0)

    response = await client.get(f"/api/v1/products/{subject.slug}/related")

    assert response.status_code == 200
    ids = [item["id"] for item in response.json()]
    assert good.id in ids
    assert dead.id not in ids
    assert empty.id not in ids


async def test_cooccurrence_then_category_fallback_combined(
    client, db_session, product_factory, category_factory
):
    """Co-occurrence results lead; same-category peers fill the rest, no dupes."""
    cat = category_factory(slug="rel-combo-cat")
    subject = product_factory(slug="rel-combo-subject", category=cat)
    bought = product_factory(slug="rel-combo-bought", category=cat)
    peer = product_factory(slug="rel-combo-peer", category=cat)

    _make_order(db_session, product_ids=[subject.id, bought.id])

    response = await client.get(f"/api/v1/products/{subject.slug}/related")

    assert response.status_code == 200
    ids = [item["id"] for item in response.json()]
    # Co-occurrence first, then the remaining same-category peer; no duplicates.
    assert ids[0] == bought.id
    assert peer.id in ids
    assert len(ids) == len(set(ids))
    assert subject.id not in ids


# ---------------------------------------------------------------------------
# Empty / limit / 404
# ---------------------------------------------------------------------------


async def test_empty_when_no_signal_and_no_peers(client, db_session, product_factory):
    """A lone product (own category, no orders) has nothing related."""
    product = product_factory(slug="rel-lonely")

    response = await client.get(f"/api/v1/products/{product.slug}/related")

    assert response.status_code == 200
    assert response.json() == []


async def test_limit_is_respected(client, db_session, product_factory, category_factory):
    cat = category_factory(slug="rel-limit-cat")
    subject = product_factory(slug="rel-limit-subject", category=cat)
    for i in range(5):
        product_factory(slug=f"rel-limit-peer-{i}", category=cat)

    response = await client.get(f"/api/v1/products/{subject.slug}/related?limit=2")

    assert response.status_code == 200
    assert len(response.json()) == 2


async def test_limit_above_max_is_rejected(client, product_factory):
    product = product_factory(slug="rel-limit-max")
    response = await client.get(f"/api/v1/products/{product.slug}/related?limit=25")
    assert response.status_code == 422


async def test_unknown_slug_returns_404(client):
    response = await client.get("/api/v1/products/does-not-exist/related")
    assert response.status_code == 404


async def test_inactive_subject_returns_404(client, product_factory):
    product = product_factory(slug="rel-inactive-subject", is_active=False)
    response = await client.get(f"/api/v1/products/{product.slug}/related")
    assert response.status_code == 404
