"""Unit tests for purchasable gift-card products (gift cards v2, DB-backed).

Covers the checkout-side behaviour that ``is_gift_card`` products introduce:
non-taxable line snapshot, all-gift-card orders skipping shipping, the physical
line still forcing shipping, gift cards being excluded from discounts, and the
issuance-on-payment path (``mark_paid`` mints one card per unit, idempotently,
with recipient + provenance).
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.crud import order as crud_order
from app.crud.order import ShippingRequiredError
from app.models.gift_card import GiftCard
from app.models.order import OrderStatus
from app.schemas.order import OrderCreate, OrderItemCreate
from tests.factories.coupon import CouponFactory

pytestmark = pytest.mark.integration


@pytest.fixture
def coupon_factory(db_session: Session):
    CouponFactory._meta.sqlalchemy_session = db_session  # pyright: ignore[reportAttributeAccessIssue]
    CouponFactory._meta.sqlalchemy_session_persistence = "commit"  # pyright: ignore[reportAttributeAccessIssue]

    def _make(**kwargs):
        return CouponFactory(**kwargs)

    return _make


def _gift_card_order(
    items: list[tuple[int, int]],
    *,
    currency: str = "USD",
    recipient_email: str | None = None,
    message: str | None = None,
    guest_email: str | None = None,
) -> OrderCreate:
    """Build an all-gift-card OrderCreate with no shipping details."""
    return OrderCreate(
        items=[
            OrderItemCreate(
                product_id=pid,
                quantity=qty,
                gift_card_recipient_email=recipient_email,  # type: ignore[arg-type]
                gift_card_message=message,
            )
            for pid, qty in items
        ],
        currency=currency,
        guest_email=guest_email,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# line snapshot + non-taxable
# ---------------------------------------------------------------------------


def test_gift_card_line_snapshots_flag_and_zero_vat(db_session, user_factory, product_factory):
    user = user_factory()
    card_product = product_factory(is_gift_card=True, stock=100, price_usd=50.0, vat_rate=23)
    order = crud_order.create_with_items(
        db_session, user_id=user.id, obj_in=_gift_card_order([(card_product.id, 1)])
    )
    (line,) = order.items
    assert line.is_gift_card is True
    # MPV voucher: the sale carries no VAT even though the product's category is 23%.
    assert line.vat_rate == 0
    assert line.unit_price == 50.0


# ---------------------------------------------------------------------------
# shipping is skipped for an all-gift-card cart, required for a physical line
# ---------------------------------------------------------------------------


def test_all_gift_card_order_skips_shipping(db_session, user_factory, product_factory):
    user = user_factory()
    card_product = product_factory(is_gift_card=True, stock=100, price_usd=25.0)
    order = crud_order.create_with_items(
        db_session, user_id=user.id, obj_in=_gift_card_order([(card_product.id, 2)])
    )
    assert order.shipping == 0.0
    assert order.shipping_method_id is None
    assert order.shipping_method_name is None
    assert order.shipping_country is None
    # NOT-NULL columns fall back to "" rather than an address.
    assert order.shipping_address == ""
    assert order.total == 50.0


def test_physical_line_without_shipping_raises(db_session, user_factory, product_factory):
    user = user_factory()
    physical = product_factory(is_gift_card=False, stock=10, price_usd=20.0)
    with pytest.raises(ShippingRequiredError):
        crud_order.create_with_items(
            db_session, user_id=user.id, obj_in=_gift_card_order([(physical.id, 1)])
        )


def test_mixed_cart_resolves_shipping_for_physical_goods(
    db_session, user_factory, product_factory, test_shipping_method_id
):
    """A cart with a physical line + a gift-card line still ships the physical
    goods (the gift-card line just contributes 0 weight)."""
    user = user_factory()
    physical = product_factory(is_gift_card=False, stock=10, price_usd=20.0)
    card_product = product_factory(is_gift_card=True, stock=100, price_usd=50.0)
    obj_in = OrderCreate(
        items=[
            OrderItemCreate(product_id=physical.id, quantity=1),
            OrderItemCreate(product_id=card_product.id, quantity=1),
        ],
        currency="USD",
        shipping_address="1 Test St",
        shipping_city="Testville",
        shipping_postal_code="00-001",
        shipping_country="PL",
        shipping_method_id=test_shipping_method_id,
    )
    order = crud_order.create_with_items(db_session, user_id=user.id, obj_in=obj_in)
    assert order.shipping_method_id == test_shipping_method_id
    assert order.subtotal == 70.0
    gift_line = next(i for i in order.items if i.is_gift_card)
    assert gift_line.vat_rate == 0


# ---------------------------------------------------------------------------
# gift cards are excluded from discounts (Shopify parity)
# ---------------------------------------------------------------------------


def test_coupon_excludes_gift_card_from_discount(
    db_session, user_factory, product_factory, coupon_factory, test_shipping_method_id
):
    """An automatic 10% coupon discounts only the physical goods, never the
    gift-card line."""
    coupon_factory(discount_type="percent", value=10.0, is_automatic=True)
    user = user_factory()
    physical = product_factory(is_gift_card=False, stock=10, price_usd=100.0)
    card_product = product_factory(is_gift_card=True, stock=100, price_usd=50.0)
    obj_in = OrderCreate(
        items=[
            OrderItemCreate(product_id=physical.id, quantity=1),
            OrderItemCreate(product_id=card_product.id, quantity=1),
        ],
        currency="USD",
        shipping_address="1 Test St",
        shipping_city="Testville",
        shipping_postal_code="00-001",
        shipping_country="PL",
        shipping_method_id=test_shipping_method_id,
    )
    order = crud_order.create_with_items(db_session, user_id=user.id, obj_in=obj_in)
    # 10% of the $100 physical line only — NOT of the $150 subtotal.
    assert order.subtotal == 150.0
    assert order.discount == 10.0
    assert order.total == 140.0


def test_coupon_gives_zero_on_all_gift_card_order(
    db_session, user_factory, product_factory, coupon_factory
):
    coupon_factory(discount_type="percent", value=10.0, is_automatic=True)
    user = user_factory()
    card_product = product_factory(is_gift_card=True, stock=100, price_usd=50.0)
    order = crud_order.create_with_items(
        db_session, user_id=user.id, obj_in=_gift_card_order([(card_product.id, 1)])
    )
    assert order.discount == 0.0
    assert order.total == 50.0


# ---------------------------------------------------------------------------
# issuance on payment (mark_paid)
# ---------------------------------------------------------------------------


def _cards_for(db_session, order) -> list[GiftCard]:
    return db_session.query(GiftCard).filter(GiftCard.order_id == order.id).all()


def test_mark_paid_issues_one_card_per_unit(
    db_session, user_factory, product_factory, captured_emails
):
    user = user_factory(email="buyer@example.com")
    card_product = product_factory(is_gift_card=True, stock=100, price_usd=50.0)
    order = crud_order.create_with_items(
        db_session,
        user_id=user.id,
        obj_in=_gift_card_order(
            [(card_product.id, 2)], recipient_email="lucky@example.com", message="Happy birthday!"
        ),
    )
    crud_order.mark_paid(db_session, order=order, payment_intent_id="pi_1", payment_method="stripe")
    db_session.flush()

    cards = _cards_for(db_session, order)
    assert len(cards) == 2
    for card in cards:
        assert card.initial_value == 50.0
        assert card.balance == 50.0
        assert card.currency == "USD"
        assert card.recipient_email == "lucky@example.com"
        assert card.user_id == user.id
    # Both issue emails went to the recipient, carrying the plaintext code.
    sent = [e for e in captured_emails if e["email_to"] == "lucky@example.com"]
    assert len(sent) == 2


def test_mark_paid_recipient_falls_back_to_buyer(
    db_session, user_factory, product_factory, captured_emails
):
    user = user_factory(email="self@example.com")
    card_product = product_factory(is_gift_card=True, stock=100, price_usd=30.0)
    order = crud_order.create_with_items(
        db_session, user_id=user.id, obj_in=_gift_card_order([(card_product.id, 1)])
    )
    crud_order.mark_paid(db_session, order=order, payment_intent_id="pi_2", payment_method="stripe")
    db_session.flush()
    (card,) = _cards_for(db_session, order)
    assert card.recipient_email == "self@example.com"


def test_mark_paid_is_idempotent_no_double_issue(
    db_session, user_factory, product_factory, captured_emails
):
    user = user_factory()
    card_product = product_factory(is_gift_card=True, stock=100, price_usd=40.0)
    order = crud_order.create_with_items(
        db_session, user_id=user.id, obj_in=_gift_card_order([(card_product.id, 1)])
    )
    crud_order.mark_paid(db_session, order=order, payment_intent_id="pi_3", payment_method="stripe")
    db_session.flush()
    # A duplicate/late notification is a no-op (status already PAID).
    crud_order.mark_paid(db_session, order=order, payment_intent_id="pi_3", payment_method="stripe")
    db_session.flush()
    assert len(_cards_for(db_session, order)) == 1


def test_physical_order_issues_no_cards(
    db_session, user_factory, product_factory, test_shipping_method_id, captured_emails
):
    from app.schemas.order import OrderCreate as OC
    from app.schemas.order import OrderItemCreate as OIC

    user = user_factory()
    physical = product_factory(is_gift_card=False, stock=10, price_usd=20.0)
    order = crud_order.create_with_items(
        db_session,
        user_id=user.id,
        obj_in=OC(
            items=[OIC(product_id=physical.id, quantity=1)],
            currency="USD",
            shipping_address="1 Test St",
            shipping_city="Testville",
            shipping_postal_code="00-001",
            shipping_country="PL",
            shipping_method_id=test_shipping_method_id,
        ),
    )
    crud_order.mark_paid(db_session, order=order, payment_intent_id="pi_4", payment_method="stripe")
    db_session.flush()
    assert _cards_for(db_session, order) == []


def test_guest_can_buy_gift_card_without_shipping(db_session, product_factory, captured_emails):
    card_product = product_factory(is_gift_card=True, stock=100, price_usd=15.0)
    order = crud_order.create_with_items(
        db_session,
        user_id=None,
        obj_in=_gift_card_order([(card_product.id, 1)], guest_email="guest@example.com"),
    )
    assert order.user_id is None
    assert order.shipping_method_id is None
    crud_order.mark_paid(db_session, order=order, payment_intent_id="pi_5", payment_method="stripe")
    db_session.flush()
    (card,) = _cards_for(db_session, order)
    # No explicit recipient → the guest's contact email receives the card.
    assert card.recipient_email == "guest@example.com"
    assert card.user_id is None
    assert order.status == OrderStatus.PAID
