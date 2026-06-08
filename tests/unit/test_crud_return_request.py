"""Unit tests for ``app/crud/return_request.py``.

Covers the eligibility math (delivered-only, window from ``delivered_at`` with
the legacy ``updated_at`` fallback, returns-disabled), the returnable-qty
formula (prior non-rejected returns subtract; rejected returns give quantity
back; refunds NOT linked from a return subtract; the linked-refund exclusion
prevents double-counting), the create-path validation errors, and the status
transition matrix. Mirrors the shape of ``tests/unit/test_crud_refund.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.crud import refund as crud_refund
from app.crud import return_request as crud_return_request
from app.crud.return_request import (
    OrderNotReturnableError,
    ReturnLineNotOnOrderError,
    ReturnLineOverError,
    ReturnsDisabledError,
    ReturnTransitionError,
    ReturnWindowClosedError,
)
from app.models.order import Order, OrderItem, OrderStatus
from app.models.refund import RefundReason
from app.models.return_request import ReturnReason, ReturnStatus
from app.schemas.return_request import ReturnItemCreate, ReturnRequestCreate


def _utcnow_naive() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _make_order(
    db_session,
    *,
    user,
    product,
    status: OrderStatus = OrderStatus.DELIVERED,
    quantity: int = 3,
    delivered_at: datetime | None = None,
) -> Order:
    """Persist a delivered-shape order without going through the route layer."""
    order = Order(
        user_id=user.id,
        order_number=f"ORD-RET-{product.id:04d}",
        status=status,
        subtotal=product.price_usd * quantity,
        tax=0.0,
        shipping=0.0,
        total=product.price_usd * quantity,
        currency="USD",
        shipping_address="1 Test St",
        shipping_city="Testville",
        shipping_postal_code="00-001",
        shipping_country="PL",
        delivered_at=delivered_at,
        items=[
            OrderItem(
                product_id=product.id,
                product_name=product.name,
                product_sku=product.sku,
                quantity=quantity,
                unit_price=product.price_usd,
                total_price=product.price_usd * quantity,
            )
        ],
    )
    db_session.add(order)
    db_session.flush()
    return order


def _request_body(order: Order, *, quantity: int = 1) -> ReturnRequestCreate:
    return ReturnRequestCreate(
        items=[
            ReturnItemCreate(
                order_item_id=order.items[0].id,
                quantity=quantity,
                reason=ReturnReason.DEFECTIVE.value,
            )
        ],
        comment="Unit-test return",
    )


# ----------------------------------------------------------------------
# Eligibility / window math
# ----------------------------------------------------------------------


def test_eligibility_requires_delivered_status(db_session, user_factory, product_factory):
    user = user_factory()
    product = product_factory(stock=10, price_usd=10.0)
    order = _make_order(db_session, user=user, product=product, status=OrderStatus.SHIPPED)

    eligible, reason, ends_at, lines = crud_return_request.check_eligibility(
        db_session, order=order, window_days=30
    )
    assert eligible is False
    assert reason == "not_delivered"
    assert ends_at is None
    assert lines == {}


def test_eligibility_zero_window_means_disabled(db_session, user_factory, product_factory):
    user = user_factory()
    product = product_factory(stock=10, price_usd=10.0)
    order = _make_order(db_session, user=user, product=product, delivered_at=_utcnow_naive())

    eligible, reason, ends_at, _ = crud_return_request.check_eligibility(
        db_session, order=order, window_days=0
    )
    assert eligible is False
    assert reason == "returns_disabled"
    assert ends_at is None


def test_eligibility_window_from_delivered_at(db_session, user_factory, product_factory):
    user = user_factory()
    product = product_factory(stock=10, price_usd=10.0)
    delivered = _utcnow_naive() - timedelta(days=10)
    order = _make_order(db_session, user=user, product=product, delivered_at=delivered)

    eligible, reason, ends_at, lines = crud_return_request.check_eligibility(
        db_session, order=order, window_days=30
    )
    assert eligible is True
    assert reason is None
    assert ends_at == delivered + timedelta(days=30)
    assert lines[order.items[0].id] == 3


def test_eligibility_window_closed_past_delivered_at(db_session, user_factory, product_factory):
    user = user_factory()
    product = product_factory(stock=10, price_usd=10.0)
    delivered = _utcnow_naive() - timedelta(days=31)
    order = _make_order(db_session, user=user, product=product, delivered_at=delivered)

    eligible, reason, ends_at, _ = crud_return_request.check_eligibility(
        db_session, order=order, window_days=30
    )
    assert eligible is False
    assert reason == "window_closed"
    assert ends_at == delivered + timedelta(days=30)


def test_eligibility_falls_back_to_updated_at_for_legacy_rows(
    db_session, user_factory, product_factory
):
    """A pre-feature delivered order (``delivered_at`` NULL) anchors on
    ``updated_at`` — a recently-updated legacy order is inside the window."""
    user = user_factory()
    product = product_factory(stock=10, price_usd=10.0)
    order = _make_order(db_session, user=user, product=product, delivered_at=None)
    db_session.refresh(order)  # naive-UTC values, as a real read would see
    assert order.delivered_at is None

    anchor = crud_return_request.delivery_anchor(order)
    assert anchor == order.updated_at

    eligible, _reason, ends_at, _ = crud_return_request.check_eligibility(
        db_session, order=order, window_days=30
    )
    assert eligible is True
    assert ends_at == order.updated_at + timedelta(days=30)


def test_eligibility_nothing_returnable_after_full_return(
    db_session, user_factory, product_factory
):
    user = user_factory()
    product = product_factory(stock=10, price_usd=10.0)
    order = _make_order(
        db_session, user=user, product=product, quantity=2, delivered_at=_utcnow_naive()
    )
    crud_return_request.create_for_order(
        db_session,
        order=order,
        user_id=user.id,
        obj_in=_request_body(order, quantity=2),
        window_days=30,
    )

    eligible, reason, _, lines = crud_return_request.check_eligibility(
        db_session, order=order, window_days=30
    )
    assert eligible is False
    assert reason == "nothing_returnable"
    assert lines[order.items[0].id] == 0


# ----------------------------------------------------------------------
# Returnable-qty formula
# ----------------------------------------------------------------------


def test_returnable_qty_subtracts_non_rejected_returns(db_session, user_factory, product_factory):
    user = user_factory()
    product = product_factory(stock=10, price_usd=10.0)
    order = _make_order(
        db_session, user=user, product=product, quantity=3, delivered_at=_utcnow_naive()
    )
    line_id = order.items[0].id

    crud_return_request.create_for_order(
        db_session,
        order=order,
        user_id=user.id,
        obj_in=_request_body(order, quantity=2),
        window_days=30,
    )
    returnable = crud_return_request.returnable_qty_by_line(db_session, order=order)
    assert returnable[line_id] == 1


def test_returnable_qty_rejected_return_gives_quantity_back(
    db_session, user_factory, product_factory
):
    user = user_factory()
    product = product_factory(stock=10, price_usd=10.0)
    order = _make_order(
        db_session, user=user, product=product, quantity=3, delivered_at=_utcnow_naive()
    )
    line_id = order.items[0].id

    ret = crud_return_request.create_for_order(
        db_session,
        order=order,
        user_id=user.id,
        obj_in=_request_body(order, quantity=2),
        window_days=30,
    )
    crud_return_request.apply_transition(
        db_session, ret=ret, target=ReturnStatus.REJECTED, admin_note="no"
    )
    db_session.flush()

    returnable = crud_return_request.returnable_qty_by_line(db_session, order=order)
    assert returnable[line_id] == 3


def test_returnable_qty_subtracts_unlinked_refunds(db_session, user_factory, product_factory):
    """A direct admin refund (NOT linked from any return) consumes returnable qty."""
    user = user_factory()
    admin = user_factory(email="ret-unit-admin1@example.com")
    product = product_factory(stock=10, price_usd=10.0)
    order = _make_order(
        db_session, user=user, product=product, quantity=3, delivered_at=_utcnow_naive()
    )
    line_id = order.items[0].id

    crud_refund.create_for_order(
        db_session,
        order=order,
        amount=10.0,
        reason=RefundReason.CUSTOMER_REQUEST.value,
        note=None,
        restock=False,
        actor_user_id=admin.id,
        items=[(line_id, 1)],
    )

    returnable = crud_return_request.returnable_qty_by_line(db_session, order=order)
    assert returnable[line_id] == 2


def test_returnable_qty_excludes_refund_linked_to_return(db_session, user_factory, product_factory):
    """The refund issued *for* a return must not double-count against the line:
    return (2 units, non-rejected) + its linked refund (2 units) subtract once."""
    user = user_factory()
    admin = user_factory(email="ret-unit-admin2@example.com")
    product = product_factory(stock=10, price_usd=10.0)
    order = _make_order(
        db_session, user=user, product=product, quantity=3, delivered_at=_utcnow_naive()
    )
    line_id = order.items[0].id

    ret = crud_return_request.create_for_order(
        db_session,
        order=order,
        user_id=user.id,
        obj_in=_request_body(order, quantity=2),
        window_days=30,
    )
    crud_return_request.apply_transition(db_session, ret=ret, target=ReturnStatus.APPROVED)
    crud_return_request.apply_transition(db_session, ret=ret, target=ReturnStatus.RECEIVED)
    refund = crud_refund.create_for_order(
        db_session,
        order=order,
        amount=20.0,
        reason=RefundReason.CUSTOMER_REQUEST.value,
        note=None,
        restock=False,
        actor_user_id=admin.id,
        items=[(line_id, 2)],
    )
    crud_return_request.mark_refunded(db_session, ret=ret, refund_id=refund.id)
    db_session.flush()

    returnable = crud_return_request.returnable_qty_by_line(db_session, order=order)
    # 3 - 2 (return) - 0 (linked refund excluded) = 1, NOT 3 - 2 - 2 = -1 → 0.
    assert returnable[line_id] == 1


# ----------------------------------------------------------------------
# Create-path validation
# ----------------------------------------------------------------------


def test_create_rejects_non_delivered_order(db_session, user_factory, product_factory):
    user = user_factory()
    product = product_factory(stock=10, price_usd=10.0)
    order = _make_order(db_session, user=user, product=product, status=OrderStatus.PAID)

    with pytest.raises(OrderNotReturnableError):
        crud_return_request.create_for_order(
            db_session, order=order, user_id=user.id, obj_in=_request_body(order), window_days=30
        )


def test_create_rejects_when_returns_disabled(db_session, user_factory, product_factory):
    user = user_factory()
    product = product_factory(stock=10, price_usd=10.0)
    order = _make_order(db_session, user=user, product=product, delivered_at=_utcnow_naive())

    with pytest.raises(ReturnsDisabledError):
        crud_return_request.create_for_order(
            db_session, order=order, user_id=user.id, obj_in=_request_body(order), window_days=0
        )


def test_create_rejects_closed_window(db_session, user_factory, product_factory):
    user = user_factory()
    product = product_factory(stock=10, price_usd=10.0)
    order = _make_order(
        db_session,
        user=user,
        product=product,
        delivered_at=_utcnow_naive() - timedelta(days=40),
    )

    with pytest.raises(ReturnWindowClosedError):
        crud_return_request.create_for_order(
            db_session, order=order, user_id=user.id, obj_in=_request_body(order), window_days=30
        )


def test_create_rejects_foreign_line(db_session, user_factory, product_factory):
    user = user_factory()
    product = product_factory(stock=10, price_usd=10.0)
    other_product = product_factory(stock=10, price_usd=5.0)
    order = _make_order(db_session, user=user, product=product, delivered_at=_utcnow_naive())
    other_order = _make_order(
        db_session, user=user, product=other_product, delivered_at=_utcnow_naive()
    )

    body = ReturnRequestCreate(
        items=[
            ReturnItemCreate(
                order_item_id=other_order.items[0].id,
                quantity=1,
                reason=ReturnReason.OTHER.value,
            )
        ],
        comment=None,
    )
    with pytest.raises(ReturnLineNotOnOrderError):
        crud_return_request.create_for_order(
            db_session, order=order, user_id=user.id, obj_in=body, window_days=30
        )


def test_create_rejects_over_quantity(db_session, user_factory, product_factory):
    user = user_factory()
    product = product_factory(stock=10, price_usd=10.0)
    order = _make_order(
        db_session, user=user, product=product, quantity=2, delivered_at=_utcnow_naive()
    )

    with pytest.raises(ReturnLineOverError):
        crud_return_request.create_for_order(
            db_session,
            order=order,
            user_id=user.id,
            obj_in=_request_body(order, quantity=3),
            window_days=30,
        )


def test_create_rejects_duplicate_line_cumulative(db_session, user_factory, product_factory):
    """The same line picked twice in one request is validated cumulatively
    (clean error before the DB UNIQUE would fire at flush)."""
    user = user_factory()
    product = product_factory(stock=10, price_usd=10.0)
    order = _make_order(
        db_session, user=user, product=product, quantity=3, delivered_at=_utcnow_naive()
    )
    line_id = order.items[0].id

    body = ReturnRequestCreate(
        items=[
            ReturnItemCreate(order_item_id=line_id, quantity=2, reason="defective"),
            ReturnItemCreate(order_item_id=line_id, quantity=2, reason="other"),
        ],
        comment=None,
    )
    with pytest.raises(ReturnLineOverError):
        crud_return_request.create_for_order(
            db_session, order=order, user_id=user.id, obj_in=body, window_days=30
        )


def test_create_happy_path_persists_request_and_lines(db_session, user_factory, product_factory):
    user = user_factory()
    product = product_factory(stock=10, price_usd=10.0)
    order = _make_order(
        db_session, user=user, product=product, quantity=3, delivered_at=_utcnow_naive()
    )

    ret = crud_return_request.create_for_order(
        db_session,
        order=order,
        user_id=user.id,
        obj_in=_request_body(order, quantity=2),
        window_days=30,
    )

    assert ret.id is not None
    assert ret.status == ReturnStatus.REQUESTED.value
    assert ret.comment == "Unit-test return"
    assert ret.refund_id is None
    assert ret.resolved_at is None
    loaded = crud_return_request.get(db_session, return_id=ret.id)
    assert loaded is not None
    assert len(loaded.items) == 1
    assert loaded.items[0].quantity == 2
    assert loaded.items[0].reason == ReturnReason.DEFECTIVE.value
    assert loaded.items[0].line_subtotal == pytest.approx(20.0)
    assert loaded.order_number == order.order_number
    assert loaded.currency == "USD"


# ----------------------------------------------------------------------
# Transition matrix
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("current", "target", "allowed"),
    [
        (ReturnStatus.REQUESTED, ReturnStatus.APPROVED, True),
        (ReturnStatus.REQUESTED, ReturnStatus.REJECTED, True),
        (ReturnStatus.REQUESTED, ReturnStatus.RECEIVED, False),
        (ReturnStatus.REQUESTED, ReturnStatus.REFUNDED, False),
        (ReturnStatus.APPROVED, ReturnStatus.RECEIVED, True),
        (ReturnStatus.APPROVED, ReturnStatus.REFUNDED, True),
        (ReturnStatus.APPROVED, ReturnStatus.REJECTED, False),
        (ReturnStatus.RECEIVED, ReturnStatus.REFUNDED, True),
        (ReturnStatus.RECEIVED, ReturnStatus.APPROVED, False),
        (ReturnStatus.REJECTED, ReturnStatus.APPROVED, False),
        (ReturnStatus.REFUNDED, ReturnStatus.RECEIVED, False),
    ],
)
def test_transition_matrix(current, target, allowed):
    error = crud_return_request.transition_error(current, target)
    assert (error is None) is allowed


def test_apply_transition_stamps_resolved_at_on_terminal(db_session, user_factory, product_factory):
    user = user_factory()
    product = product_factory(stock=10, price_usd=10.0)
    order = _make_order(db_session, user=user, product=product, delivered_at=_utcnow_naive())
    ret = crud_return_request.create_for_order(
        db_session, order=order, user_id=user.id, obj_in=_request_body(order), window_days=30
    )

    crud_return_request.apply_transition(
        db_session, ret=ret, target=ReturnStatus.REJECTED, admin_note="counterfeit claim"
    )
    assert ret.status == ReturnStatus.REJECTED.value
    assert ret.admin_note == "counterfeit claim"
    assert ret.resolved_at is not None


def test_apply_transition_raises_on_illegal_move(db_session, user_factory, product_factory):
    user = user_factory()
    product = product_factory(stock=10, price_usd=10.0)
    order = _make_order(db_session, user=user, product=product, delivered_at=_utcnow_naive())
    ret = crud_return_request.create_for_order(
        db_session, order=order, user_id=user.id, obj_in=_request_body(order), window_days=30
    )

    with pytest.raises(ReturnTransitionError):
        crud_return_request.apply_transition(db_session, ret=ret, target=ReturnStatus.RECEIVED)
    assert ret.status == ReturnStatus.REQUESTED.value
