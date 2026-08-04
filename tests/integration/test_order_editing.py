"""Integration tests for editing a pending order's items (Back-office A2).

Covers PATCH /api/v1/orders/{id}/items: add/remove/qty changes with exact
Decimal recompute + honest stock reconciliation, manual discount, shipping
re-resolution, the PENDING-only 409 guard, the emitted 'edited' timeline event,
and the orders:write capability gate.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from app.core.capabilities import CATALOG_WRITE, ORDERS_WRITE
from app.core.security import create_access_token
from app.crud import store_config as crud_store_config
from app.crud.gift_card import gift_card as crud_gift_card
from app.crud.inventory import inventory as crud_inventory
from app.crud.store_credit import store_credit as crud_store_credit
from app.models.gift_card import GiftCardEntry
from app.models.inventory_movement import InventoryMovementReason
from app.models.order import Order
from app.models.order_event import OrderEvent
from app.models.product import Product
from app.models.user import UserRole
from app.services import order_email

pytestmark = pytest.mark.integration


@pytest.fixture
def scheduled_emails(monkeypatch) -> list[tuple[int, str]]:
    calls: list[tuple[int, str]] = []
    monkeypatch.setattr(
        order_email,
        "send_order_event_in_background",
        lambda order_id, event: calls.append((order_id, event)),
    )
    return calls


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


async def _place_order(client, customer, product, *, shipping_method_id: int, quantity: int = 1):
    resp = await client.post(
        "/api/v1/orders/",
        json=_order_body(product.id, shipping_method_id=shipping_method_id, quantity=quantity),
        headers=_headers(customer),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _stock(db: Session, product_id: int) -> int:
    product = db.get(Product, product_id)
    assert product is not None
    db.refresh(product)
    return product.stock


# ---------------------------------------------------------------------------
# quantity / add / remove + recompute + stock
# ---------------------------------------------------------------------------


async def test_change_quantity_recomputes_and_reconciles_stock(
    client, user_factory, product_factory, db_session: Session, test_shipping_method_id
):
    customer = user_factory(email="oedit-c1@example.com")
    admin = user_factory(email="oedit-a1@example.com", role=UserRole.ADMIN)
    product = product_factory(stock=10, price_usd=10)
    order = await _place_order(
        client, customer, product, shipping_method_id=test_shipping_method_id, quantity=2
    )
    assert _stock(db_session, product.id) == 8  # 10 - 2

    resp = await client.patch(
        f"/api/v1/orders/{order['id']}/items",
        json={"lines": [{"product_id": product.id, "quantity": 3}]},
        headers=_headers(admin),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["items"][0]["quantity"] == 3
    assert body["subtotal"] == pytest.approx(30.0)  # 3 x 10
    # Stock: released +2 (→10), re-acquired -3 (→7).
    assert _stock(db_session, product.id) == 7


async def test_add_and_remove_lines(
    client, user_factory, product_factory, db_session: Session, test_shipping_method_id
):
    customer = user_factory(email="oedit-c2@example.com")
    admin = user_factory(email="oedit-a2@example.com", role=UserRole.ADMIN)
    p1 = product_factory(stock=10, price_usd=10)
    p2 = product_factory(stock=10, price_usd=5)
    order = await _place_order(
        client, customer, p1, shipping_method_id=test_shipping_method_id, quantity=1
    )

    # Add p2 (qty 2) alongside p1 (qty 1).
    resp = await client.patch(
        f"/api/v1/orders/{order['id']}/items",
        json={
            "lines": [
                {"product_id": p1.id, "quantity": 1},
                {"product_id": p2.id, "quantity": 2},
            ]
        },
        headers=_headers(admin),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["subtotal"] == pytest.approx(20.0)  # 10 + 2x5
    assert _stock(db_session, p2.id) == 8

    # Remove p1 (only p2 remains).
    resp = await client.patch(
        f"/api/v1/orders/{order['id']}/items",
        json={"lines": [{"product_id": p2.id, "quantity": 2}]},
        headers=_headers(admin),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["items"]) == 1
    assert body["items"][0]["product_id"] == p2.id
    # p1's stock fully released back to 10.
    assert _stock(db_session, p1.id) == 10


async def test_manual_discount_applied_to_total(
    client, user_factory, product_factory, test_shipping_method_id
):
    customer = user_factory(email="oedit-c3@example.com")
    admin = user_factory(email="oedit-a3@example.com", role=UserRole.ADMIN)
    product = product_factory(stock=10, price_usd=100)
    order = await _place_order(
        client, customer, product, shipping_method_id=test_shipping_method_id, quantity=1
    )
    shipping = order["shipping"]

    resp = await client.patch(
        f"/api/v1/orders/{order['id']}/items",
        json={"lines": [{"product_id": product.id, "quantity": 1}], "discount": 15},
        headers=_headers(admin),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["discount"] == pytest.approx(15.0)
    # total = subtotal(100) - discount(15) + shipping
    assert body["total"] == pytest.approx(100.0 - 15.0 + shipping)


async def test_price_override(client, user_factory, product_factory, test_shipping_method_id):
    customer = user_factory(email="oedit-c4@example.com")
    admin = user_factory(email="oedit-a4@example.com", role=UserRole.ADMIN)
    product = product_factory(stock=10, price_usd=100)
    order = await _place_order(
        client, customer, product, shipping_method_id=test_shipping_method_id, quantity=1
    )

    resp = await client.patch(
        f"/api/v1/orders/{order['id']}/items",
        json={"lines": [{"product_id": product.id, "quantity": 2, "unit_price_override": 40}]},
        headers=_headers(admin),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["items"][0]["unit_price"] == pytest.approx(40.0)
    assert body["subtotal"] == pytest.approx(80.0)


# ---------------------------------------------------------------------------
# guards + event + gating
# ---------------------------------------------------------------------------


async def test_non_pending_order_409(
    client, user_factory, product_factory, test_shipping_method_id, scheduled_emails
):
    customer = user_factory(email="oedit-c5@example.com")
    admin = user_factory(email="oedit-a5@example.com", role=UserRole.ADMIN)
    product = product_factory(stock=10, price_usd=10)
    order = await _place_order(
        client, customer, product, shipping_method_id=test_shipping_method_id, quantity=1
    )
    # Flip to paid → no longer editable.
    paid = await client.patch(
        f"/api/v1/orders/{order['id']}",
        json={"status": "paid", "payment_method": "stripe"},
        headers=_headers(admin),
    )
    assert paid.status_code == 200, paid.text

    resp = await client.patch(
        f"/api/v1/orders/{order['id']}/items",
        json={"lines": [{"product_id": product.id, "quantity": 5}]},
        headers=_headers(admin),
    )
    assert resp.status_code == 409, resp.text


async def test_edited_event_emitted(
    client, user_factory, product_factory, db_session: Session, test_shipping_method_id
):
    customer = user_factory(email="oedit-c6@example.com")
    admin = user_factory(email="oedit-a6@example.com", role=UserRole.ADMIN)
    product = product_factory(stock=10, price_usd=10)
    order = await _place_order(
        client, customer, product, shipping_method_id=test_shipping_method_id, quantity=1
    )

    resp = await client.patch(
        f"/api/v1/orders/{order['id']}/items",
        json={"lines": [{"product_id": product.id, "quantity": 4}]},
        headers=_headers(admin),
    )
    assert resp.status_code == 200, resp.text

    edited = (
        db_session.query(OrderEvent)
        .filter(OrderEvent.order_id == order["id"], OrderEvent.kind == "edited")
        .one()
    )
    assert edited.actor_id == admin.id
    assert edited.payload["before"][0]["quantity"] == 1
    assert edited.payload["after"][0]["quantity"] == 4


async def test_edit_requires_orders_write_capability(
    client, user_factory, product_factory, test_shipping_method_id
):
    customer = user_factory(email="oedit-c7@example.com")
    product = product_factory(stock=10, price_usd=10)
    order = await _place_order(
        client, customer, product, shipping_method_id=test_shipping_method_id, quantity=1
    )

    no_cap = user_factory(
        email="oedit-nocap@example.com",
        role=UserRole.ADMIN,
        is_owner=False,
        capabilities=[CATALOG_WRITE],
    )
    denied = await client.patch(
        f"/api/v1/orders/{order['id']}/items",
        json={"lines": [{"product_id": product.id, "quantity": 2}]},
        headers=_headers(no_cap),
    )
    assert denied.status_code == 403

    with_cap = user_factory(
        email="oedit-withcap@example.com",
        role=UserRole.ADMIN,
        is_owner=False,
        capabilities=[ORDERS_WRITE],
    )
    allowed = await client.patch(
        f"/api/v1/orders/{order['id']}/items",
        json={"lines": [{"product_id": product.id, "quantity": 2}]},
        headers=_headers(with_cap),
    )
    assert allowed.status_code == 200


async def test_edit_404(client, user_factory):
    admin = user_factory(email="oedit-a8@example.com", role=UserRole.ADMIN)
    resp = await client.patch(
        "/api/v1/orders/999999/items",
        json={"lines": [{"product_id": 1, "quantity": 1}]},
        headers=_headers(admin),
    )
    assert resp.status_code == 404


async def test_insufficient_stock_422(
    client, user_factory, product_factory, test_shipping_method_id
):
    customer = user_factory(email="oedit-c9@example.com")
    admin = user_factory(email="oedit-a9@example.com", role=UserRole.ADMIN)
    product = product_factory(stock=3, price_usd=10)
    order = await _place_order(
        client, customer, product, shipping_method_id=test_shipping_method_id, quantity=1
    )
    # Ask for 10 units of a product with only 3 in stock (1 already held → 3 avail).
    resp = await client.patch(
        f"/api/v1/orders/{order['id']}/items",
        json={"lines": [{"product_id": product.id, "quantity": 10}]},
        headers=_headers(admin),
    )
    assert resp.status_code == 422, resp.text


async def test_edit_releases_line_of_oversold_product(
    client, user_factory, product_factory, db_session: Session, test_shipping_method_id
):
    """S2 regression: editing a pending order whose product is already oversold
    (warehouse level negative from a prior backorder) must not 500.

    The release leg gives stock back (positive delta); with the level negative it
    only *reduces* the deficit (still < 0). The apply_delta "cannot go negative"
    guard used to fire on that pure give-back → ValueError → order un-editable.
    """
    customer = user_factory(email="oedit-c10@example.com")
    admin = user_factory(email="oedit-a10@example.com", role=UserRole.ADMIN)
    # allow_backorder so the post-release rebuild's availability check passes.
    product = product_factory(stock=1, price_usd=10, allow_backorder=True)
    order = await _place_order(
        client, customer, product, shipping_method_id=test_shipping_method_id, quantity=1
    )
    # Drive the default-warehouse level deep negative (simulates prior backorders).
    crud_inventory.apply_delta(
        db_session,
        product=product,
        delta=-19,
        reason=InventoryMovementReason.MANUAL_ADJUSTMENT,
        allow_negative=True,
        note="oversold seed",
    )
    db_session.commit()
    assert _stock(db_session, product.id) == -19  # 0 (post-order) - 19

    # Re-edit the order (bump qty to 2). Release leg (+1 → -18) must not raise.
    resp = await client.patch(
        f"/api/v1/orders/{order['id']}/items",
        json={"lines": [{"product_id": product.id, "quantity": 2}]},
        headers=_headers(admin),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["items"][0]["quantity"] == 2
    # Net: released +1 (→ -18), re-acquired -2 (→ -20).
    assert _stock(db_session, product.id) == -20


# ---------------------------------------------------------------------------
# stored-value tender reconciliation (SEC-019)
# ---------------------------------------------------------------------------


@pytest.fixture
def toggles_on(db_session: Session):
    """Enable both stored-value toggles for this test (they ship dark)."""
    config = crud_store_config.get_singleton(db_session)
    config.gift_cards_enabled = True
    config.store_credit_enabled = True
    db_session.commit()
    return config


def _issue_card(db: Session, value: str, currency: str = "USD"):
    card, code = crud_gift_card.issue(db, initial_value=Decimal(value), currency=currency)
    db.commit()
    return card, code


def _grant_credit(db: Session, user, amount: str, currency: str = "USD") -> None:
    crud_store_credit.adjust(db, user_id=user.id, currency=currency, amount=Decimal(amount))
    db.commit()


async def _place_tendered_order(
    client, customer, product, *, shipping_method_id: int, quantity: int, **tender
):
    body = _order_body(product.id, shipping_method_id=shipping_method_id, quantity=quantity)
    body.update(tender)
    resp = await client.post("/api/v1/orders/", json=body, headers=_headers(customer))
    assert resp.status_code == 201, resp.text
    return resp.json()


def _release_rows(db: Session, order_id: int) -> list[GiftCardEntry]:
    return (
        db.query(GiftCardEntry)
        .filter(GiftCardEntry.order_id == order_id, GiftCardEntry.kind == "release")
        .all()
    )


async def test_edit_down_releases_gift_card_excess_and_settles(
    client, user_factory, product_factory, db_session: Session, toggles_on, test_shipping_method_id
):
    """An order edited below its tendered amount returns the excess and settles.

    Before SEC-019 the snapshot stayed frozen while ``total`` fell, so
    ``amount_due`` went negative — an amount no provider webhook can ever match
    (``_paid_amount_matches`` rounds it to minor units), wedging the order in
    PENDING with the customer's gift-card balance still consumed.
    """
    customer = user_factory(email="oedit-t1@example.com")
    admin = user_factory(email="oedit-t1a@example.com", role=UserRole.ADMIN)
    product = product_factory(stock=10, price_usd=100)
    card, code = _issue_card(db_session, "150.00")

    order = await _place_tendered_order(
        client,
        customer,
        product,
        shipping_method_id=test_shipping_method_id,
        quantity=2,
        gift_card_code=code,
    )
    assert (order["total"], order["gift_card_amount"], order["amount_due"]) == (200.0, 150.0, 50.0)
    assert order["status"] == "pending"

    resp = await client.patch(
        f"/api/v1/orders/{order['id']}/items",
        json={"lines": [{"product_id": product.id, "quantity": 1}]},
        headers=_headers(admin),
    )
    assert resp.status_code == 200, resp.text
    edited = resp.json()
    assert edited["total"] == 100.0
    assert edited["gift_card_amount"] == 100.0  # 150 tendered - 50 excess
    assert edited["amount_due"] == 0.0
    # Re-fitting the tender always lands it exactly on the new total, so any
    # release leaves nothing for a provider to collect — the order settles here.
    assert edited["status"] == "paid"
    assert edited["payment_method"] == "gift_card"
    assert edited["payment_intent_id"] is None

    db_session.refresh(card)
    assert card.balance == Decimal("50.00")  # 150 - 150 debited + 50 released
    releases = _release_rows(db_session, order["id"])
    assert [e.amount for e in releases] == [Decimal("50.00")]


async def test_edit_down_unwinds_store_credit_before_gift_card(
    client, user_factory, product_factory, db_session: Session, toggles_on, test_shipping_method_id
):
    """Unwinding is LIFO — the inverse of checkout's card-first application.

    Checkout takes the gift card first and store credit on the remainder, so the
    excess must come off store credit first; the surviving split is then the one
    checkout would itself have produced for the smaller total.
    """
    customer = user_factory(email="oedit-t2@example.com")
    admin = user_factory(email="oedit-t2a@example.com", role=UserRole.ADMIN)
    product = product_factory(stock=10, price_usd=100)
    card, code = _issue_card(db_session, "100.00")
    _grant_credit(db_session, customer, "100.00")

    order = await _place_tendered_order(
        client,
        customer,
        product,
        shipping_method_id=test_shipping_method_id,
        quantity=3,
        gift_card_code=code,
        use_store_credit=True,
    )
    assert (order["total"], order["amount_due"]) == (300.0, 100.0)
    assert (order["gift_card_amount"], order["store_credit_amount"]) == (100.0, 100.0)

    # 100 of tender is now excess. All of it must come off store credit.
    resp = await client.patch(
        f"/api/v1/orders/{order['id']}/items",
        json={"lines": [{"product_id": product.id, "quantity": 1}]},
        headers=_headers(admin),
    )
    assert resp.status_code == 200, resp.text
    edited = resp.json()
    assert edited["gift_card_amount"] == 100.0
    assert edited["store_credit_amount"] == 0.0
    assert edited["amount_due"] == 0.0
    assert edited["payment_method"] == "gift_card"

    db_session.refresh(card)
    assert card.balance == Decimal("0.00")  # untouched by the release
    account = crud_store_credit.get_account(db_session, user_id=customer.id, currency="USD")
    assert account is not None
    db_session.refresh(account)
    assert account.balance == Decimal("100.00")  # fully returned
    assert _release_rows(db_session, order["id"]) == []


async def test_edit_up_leaves_the_tender_untouched(
    client, user_factory, product_factory, db_session: Session, toggles_on, test_shipping_method_id
):
    """Editing upward needs no reconciliation — the customer just owes more."""
    customer = user_factory(email="oedit-t3@example.com")
    admin = user_factory(email="oedit-t3a@example.com", role=UserRole.ADMIN)
    product = product_factory(stock=10, price_usd=100)
    card, code = _issue_card(db_session, "50.00")

    order = await _place_tendered_order(
        client,
        customer,
        product,
        shipping_method_id=test_shipping_method_id,
        quantity=1,
        gift_card_code=code,
    )
    assert (order["gift_card_amount"], order["amount_due"]) == (50.0, 50.0)

    resp = await client.patch(
        f"/api/v1/orders/{order['id']}/items",
        json={"lines": [{"product_id": product.id, "quantity": 2}]},
        headers=_headers(admin),
    )
    assert resp.status_code == 200, resp.text
    edited = resp.json()
    assert edited["total"] == 200.0
    assert edited["gift_card_amount"] == 50.0
    assert edited["amount_due"] == 150.0
    assert edited["status"] == "pending"

    db_session.refresh(card)
    assert card.balance == Decimal("0.00")
    assert _release_rows(db_session, order["id"]) == []


async def test_edit_409s_when_the_tender_ledger_is_short(
    client, user_factory, product_factory, db_session: Session, test_shipping_method_id
):
    """A snapshot the ledger can't back fails the edit loudly instead of wedging.

    The release is clamped to what the ledger actually holds, and the snapshot is
    reduced by what actually moved — so a snapshot claiming money with no
    matching redeem row leaves ``amount_due`` negative and raises. In production
    the request's rollback leaves the order in its previous, payable shape (the
    test harness deliberately keeps the session open, so that isn't asserted here).
    """
    customer = user_factory(email="oedit-t4@example.com")
    admin = user_factory(email="oedit-t4a@example.com", role=UserRole.ADMIN)
    product = product_factory(stock=10, price_usd=100)
    order = await _place_tendered_order(
        client, customer, product, shipping_method_id=test_shipping_method_id, quantity=2
    )

    # Fabricate the disagreement: a tender snapshot with no ledger behind it.
    row = db_session.get(Order, order["id"])
    assert row is not None
    row.gift_card_amount = Decimal("500.00")
    db_session.commit()

    resp = await client.patch(
        f"/api/v1/orders/{order['id']}/items",
        json={"lines": [{"product_id": product.id, "quantity": 1}]},
        headers=_headers(admin),
    )
    assert resp.status_code == 409, resp.text
    assert "negative amount due" in resp.json()["detail"]
