"""Integration tests for the product hard-delete FK guard.

``DELETE /api/v1/products/{id}`` hard-deletes an unreferenced product (204), but
a product referenced by order history (``order_items.product_id`` is
ON DELETE RESTRICT) or quote history (``quote_items.product_id`` is SET NULL,
but we still refuse to keep history attributable) must return a clean 409 —
NOT an unhandled ``ForeignKeyViolation`` 500. Closes QA finding A-2.
"""

from __future__ import annotations

import itertools

import pytest

from app.core.security import create_access_token
from app.models.order import Order, OrderItem, OrderStatus
from app.models.quote import Quote, QuoteItem
from app.models.user import UserRole
from tests.factories.company import CompanyFactory

pytestmark = pytest.mark.integration

_order_seq = itertools.count(1)


@pytest.fixture
def company_factory(db_session):
    """Callable creating a Company row via the factory (session bound per-test)."""
    CompanyFactory._meta.sqlalchemy_session = db_session  # pyright: ignore[reportAttributeAccessIssue]
    CompanyFactory._meta.sqlalchemy_session_persistence = "commit"  # pyright: ignore[reportAttributeAccessIssue]

    def _make(**kwargs):
        return CompanyFactory(**kwargs)

    return _make


def _admin_headers(user_factory, email: str) -> dict:
    admin = user_factory(email=email, role=UserRole.ADMIN)
    token, _, _ = create_access_token(subject=str(admin.id))
    return {"Authorization": f"Bearer {token}"}


def _order_item_for(db, product_id: int, *, status: OrderStatus) -> None:
    """Insert an order with a single line item referencing ``product_id``."""
    order = Order(
        order_number=f"ORD-DEL-{next(_order_seq):05d}",
        guest_email="buyer@example.com",
        status=status,
        subtotal=50.0,
        total=50.0,
        currency="USD",
        shipping_address="1 Main St",
        shipping_city="Warsaw",
        shipping_postal_code="00-001",
        shipping_country="PL",
    )
    db.add(order)
    db.flush()
    db.add(
        OrderItem(
            order_id=order.id,
            product_id=product_id,
            product_name="Item",
            quantity=1,
            unit_price=50.0,
            total_price=50.0,
        )
    )
    db.commit()


async def test_delete_unreferenced_product_204(client, db_session, product_factory, user_factory):
    headers = _admin_headers(user_factory, "del-unref@admin.test")
    product = product_factory(slug="del-unref")
    resp = await client.delete(f"/api/v1/products/{product.id}", headers=headers)
    assert resp.status_code == 204, resp.text


async def test_delete_product_with_order_history_409(
    client, db_session, product_factory, user_factory
):
    headers = _admin_headers(user_factory, "del-order@admin.test")
    product = product_factory(slug="del-order")
    _order_item_for(db_session, product.id, status=OrderStatus.PAID)

    resp = await client.delete(f"/api/v1/products/{product.id}", headers=headers)
    assert resp.status_code == 409, resp.text
    assert "deactivate" in resp.json()["detail"].lower()


async def test_delete_product_with_cancelled_order_history_409(
    client, db_session, product_factory, user_factory
):
    """A cancelled order still pins the product — the FK row persists."""
    headers = _admin_headers(user_factory, "del-cancelled@admin.test")
    product = product_factory(slug="del-cancelled")
    _order_item_for(db_session, product.id, status=OrderStatus.CANCELLED)

    resp = await client.delete(f"/api/v1/products/{product.id}", headers=headers)
    assert resp.status_code == 409, resp.text


async def test_delete_product_with_quote_history_409(
    client, db_session, product_factory, user_factory, company_factory
):
    headers = _admin_headers(user_factory, "del-quote@admin.test")
    product = product_factory(slug="del-quote")

    company = company_factory(name="Quote Co")
    quote = Quote(
        company_id=company.id,
        status="draft",
        currency="USD",
        subtotal=10.0,
        total=10.0,
    )
    db_session.add(quote)
    db_session.flush()
    db_session.add(
        QuoteItem(
            quote_id=quote.id,
            product_id=product.id,
            description="Line",
            quantity=1,
            unit_price=10.0,
            line_total=10.0,
        )
    )
    db_session.commit()

    resp = await client.delete(f"/api/v1/products/{product.id}", headers=headers)
    assert resp.status_code == 409, resp.text
