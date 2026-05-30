"""Unit tests for the faktura-korygująca allocator.

Mirrors ``tests/unit/test_crud_invoice.py``: idempotent issuance, gap-free
sequence per year, configurable number format. The shared session here uses
SAVEPOINT isolation so each test gets a fresh counter.
"""

from __future__ import annotations

from app.crud.corrective_invoice import corrective_invoice as crud_corrective
from app.crud.invoice import invoice as crud_invoice
from app.models.corrective_invoice import CorrectiveInvoiceCounter
from app.models.order import Order, OrderItem, OrderStatus
from app.models.refund import Refund


def _make_paid_order(db_session, *, user, product, ordernum_suffix: str) -> Order:
    order = Order(
        user_id=user.id,
        order_number=f"ORD-TEST-KOR-{ordernum_suffix}",
        status=OrderStatus.PAID,
        subtotal=10.0,
        total=10.0,
        currency="USD",
        shipping_address="x",
        shipping_city="y",
        shipping_postal_code="z",
        shipping_country="PL",
        items=[
            OrderItem(
                product_id=product.id,
                product_name=product.name,
                product_sku=product.sku,
                quantity=1,
                unit_price=10.0,
                total_price=10.0,
            )
        ],
    )
    db_session.add(order)
    db_session.flush()
    return order


def _make_refund(db_session, order: Order) -> Refund:
    """Persist a refund row for an order — the KOR allocator needs a real id +
    FK target. The full CRUDRefund flow is exercised in ``test_crud_refund.py``;
    this helper just sets up the precondition cheaply."""
    r = Refund(
        order_id=order.id,
        amount=order.total,
        currency=order.currency,
        reason="customer_request",
        restock=False,
    )
    db_session.add(r)
    db_session.flush()
    return r


def test_issue_for_refund_is_idempotent(db_session, user_factory, product_factory):
    """A second call for the same refund returns the existing KOR (no second allocation)."""
    user = user_factory()
    product = product_factory(stock=5, price_usd=10.0)
    order = _make_paid_order(db_session, user=user, product=product, ordernum_suffix="IDEMP")
    invoice = crud_invoice.issue_for_order(db_session, order=order)
    refund_row = _make_refund(db_session, order)

    kor_a = crud_corrective.issue_for_refund(
        db_session, refund=refund_row, original_invoice=invoice
    )
    kor_b = crud_corrective.issue_for_refund(
        db_session, refund=refund_row, original_invoice=invoice
    )

    assert kor_a.id == kor_b.id
    assert kor_a.kor_number == kor_b.kor_number


def test_issue_for_refund_allocates_gap_free_sequence(db_session, user_factory, product_factory):
    """Two refunds for two different orders → two KORs whose seq differs by exactly 1."""
    user = user_factory()
    product = product_factory(stock=5, price_usd=10.0)

    # Two paid orders, each with its own faktura.
    o1 = _make_paid_order(db_session, user=user, product=product, ordernum_suffix="GAP1")
    o2 = _make_paid_order(db_session, user=user, product=product, ordernum_suffix="GAP2")
    inv1 = crud_invoice.issue_for_order(db_session, order=o1)
    inv2 = crud_invoice.issue_for_order(db_session, order=o2)

    r1 = _make_refund(db_session, o1)
    r2 = _make_refund(db_session, o2)

    kor1 = crud_corrective.issue_for_refund(db_session, refund=r1, original_invoice=inv1)
    kor2 = crud_corrective.issue_for_refund(db_session, refund=r2, original_invoice=inv2)

    # Same year, seq incremented by 1, distinct numbers.
    assert kor1.year == kor2.year
    assert kor2.seq == kor1.seq + 1
    assert kor1.kor_number != kor2.kor_number


def test_kor_number_renders_via_settings_template(
    db_session, user_factory, product_factory, monkeypatch
):
    """``INVOICE_KOR_NUMBER_FORMAT`` is honoured at allocation time."""
    from app.core.config import settings as app_settings

    monkeypatch.setattr(app_settings, "INVOICE_KOR_NUMBER_PREFIX", "ZZ")
    monkeypatch.setattr(app_settings, "INVOICE_KOR_NUMBER_FORMAT", "{prefix}-{year}-{seq:04d}")

    user = user_factory()
    product = product_factory(stock=5, price_usd=10.0)
    order = _make_paid_order(db_session, user=user, product=product, ordernum_suffix="FMT")
    inv = crud_invoice.issue_for_order(db_session, order=order)
    r = _make_refund(db_session, order)
    kor = crud_corrective.issue_for_refund(db_session, refund=r, original_invoice=inv)

    assert kor.kor_number.startswith("ZZ-")
    assert f"-{kor.year}-" in kor.kor_number
    assert kor.kor_number.endswith(f"{kor.seq:04d}")


def test_counter_row_increments_in_session(db_session, user_factory, product_factory):
    """The atomic counter persists ``next_seq`` so a subsequent allocation in the same
    transaction sees the incremented value (the heart of the gap-free invariant)."""
    user = user_factory()
    product = product_factory(stock=5, price_usd=10.0)
    order = _make_paid_order(db_session, user=user, product=product, ordernum_suffix="CNT")
    inv = crud_invoice.issue_for_order(db_session, order=order)
    r = _make_refund(db_session, order)
    kor = crud_corrective.issue_for_refund(db_session, refund=r, original_invoice=inv)

    counter = db_session.get(CorrectiveInvoiceCounter, kor.year)
    assert counter is not None
    assert counter.next_seq == kor.seq


def test_unstubbed_lookup_returns_none(db_session):
    """``get_by_refund_id`` returns ``None`` for a refund that has no KOR."""
    assert crud_corrective.get_by_refund_id(db_session, refund_id=999_999) is None


def test_issue_for_refund_smoke(db_session, user_factory, product_factory):
    """End-to-end smoke through the public allocator API (kept here for parity with
    ``test_crud_invoice``'s ``test_issue_for_order_smoke``)."""
    user = user_factory()
    product = product_factory(stock=5, price_usd=10.0)
    order = _make_paid_order(db_session, user=user, product=product, ordernum_suffix="SMK")
    inv = crud_invoice.issue_for_order(db_session, order=order)
    r = _make_refund(db_session, order)

    kor = crud_corrective.issue_for_refund(db_session, refund=r, original_invoice=inv)
    fetched = crud_corrective.get_by_refund_id(db_session, refund_id=r.id)
    assert fetched is not None
    assert fetched.id == kor.id
    # Stable issue date (naive UTC, project convention).
    assert kor.issued_at.tzinfo is None
