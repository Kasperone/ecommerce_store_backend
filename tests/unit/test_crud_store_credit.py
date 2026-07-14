"""Unit tests for ``crud/store_credit.py`` (DB-backed, no HTTP).

Covers the lazy account create on first grant, the signed adjust with the
negative-balance guard, per-currency account isolation, the PR2 seams
``debit`` (insufficient guard) / ``release_for_order`` (idempotent, keyed by
order_id), and the newest-first cross-account ledger read.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.crud.store_credit import StoreCreditError
from app.crud.store_credit import store_credit as crud_store_credit
from app.models.order import Order
from app.models.store_credit import StoreCreditAccount, StoreCreditEntry

pytestmark = pytest.mark.integration


@pytest.fixture
def customer(user_factory):
    return user_factory(email="sc-crud@example.com")


@pytest.fixture
def make_order_id(db_session, customer):
    """Callable minting real order ids (ledger order_id is a real FK)."""
    counter = {"n": 0}

    def _make() -> int:
        counter["n"] += 1
        order = Order(
            user_id=customer.id,
            order_number=f"SC-TEST-{customer.id}-{counter['n']}",
            subtotal=Decimal("10.00"),
            total=Decimal("10.00"),
            currency="PLN",
            shipping_address="1 Test St",
            shipping_city="Warsaw",
            shipping_postal_code="00-001",
            shipping_country="PL",
        )
        db_session.add(order)
        db_session.flush()
        return order.id

    return _make


def _grant(db, user, amount: str, currency: str = "PLN"):
    account, entry = crud_store_credit.adjust(
        db, user_id=user.id, currency=currency, amount=Decimal(amount)
    )
    db.commit()
    return account, entry


# ---------------------------------------------------------------------------
# adjust
# ---------------------------------------------------------------------------


def test_first_grant_creates_account_and_ledger(db_session, customer):
    account, entry = _grant(db_session, customer, "50.00")
    assert account.user_id == customer.id
    assert account.currency == "PLN"
    assert account.balance == Decimal("50.00")
    assert entry.kind == "adjust"
    assert entry.amount == Decimal("50.00")
    assert entry.balance_after == Decimal("50.00")


def test_adjust_accumulates_on_existing_account(db_session, customer):
    _grant(db_session, customer, "50.00")
    account, entry = _grant(db_session, customer, "-20.00")
    assert account.balance == Decimal("30.00")
    assert entry.balance_after == Decimal("30.00")
    # One account row only.
    assert (
        db_session.query(StoreCreditAccount)
        .filter(StoreCreditAccount.user_id == customer.id)
        .count()
        == 1
    )


def test_adjust_below_zero_raises(db_session, customer):
    _grant(db_session, customer, "10.00")
    with pytest.raises(StoreCreditError) as exc_info:
        crud_store_credit.adjust(
            db_session, user_id=customer.id, currency="PLN", amount=Decimal("-10.01")
        )
    assert exc_info.value.code == "store_credit_balance_negative"
    db_session.rollback()
    account = crud_store_credit.get_account(db_session, user_id=customer.id, currency="PLN")
    assert account is not None
    assert account.balance == Decimal("10.00")


def test_negative_adjust_on_missing_account_raises(db_session, customer):
    with pytest.raises(StoreCreditError) as exc_info:
        crud_store_credit.adjust(
            db_session, user_id=customer.id, currency="EUR", amount=Decimal("-5.00")
        )
    assert exc_info.value.code == "store_credit_balance_negative"


def test_accounts_are_per_currency(db_session, customer):
    _grant(db_session, customer, "10.00", currency="PLN")
    _grant(db_session, customer, "20.00", currency="USD")
    accounts = crud_store_credit.accounts_for_user(db_session, user_id=customer.id)
    assert [(a.currency, a.balance) for a in accounts] == [
        ("PLN", Decimal("10.00")),
        ("USD", Decimal("20.00")),
    ]


# ---------------------------------------------------------------------------
# debit (PR2 seam)
# ---------------------------------------------------------------------------


def test_debit_writes_redeem_entry_keyed_by_order(db_session, customer, make_order_id):
    _grant(db_session, customer, "100.00")
    order_id = make_order_id()
    account, entry = crud_store_credit.debit(
        db_session, user_id=customer.id, currency="PLN", amount=Decimal("40.00"), order_id=order_id
    )
    db_session.commit()
    assert account.balance == Decimal("60.00")
    assert entry.kind == "redeem"
    assert entry.amount == Decimal("-40.00")
    assert entry.balance_after == Decimal("60.00")
    assert entry.order_id == order_id


def test_debit_insufficient_raises(db_session, customer):
    _grant(db_session, customer, "10.00")
    with pytest.raises(StoreCreditError) as exc_info:
        crud_store_credit.debit(
            db_session, user_id=customer.id, currency="PLN", amount=Decimal("10.01"), order_id=1
        )
    assert exc_info.value.code == "store_credit_insufficient"


def test_debit_missing_account_raises_insufficient(db_session, customer):
    with pytest.raises(StoreCreditError) as exc_info:
        crud_store_credit.debit(
            db_session, user_id=customer.id, currency="EUR", amount=Decimal("1.00"), order_id=1
        )
    assert exc_info.value.code == "store_credit_insufficient"


def test_debit_rejects_non_positive_amount(db_session, customer):
    _grant(db_session, customer, "10.00")
    with pytest.raises(ValueError, match="positive"):
        crud_store_credit.debit(
            db_session, user_id=customer.id, currency="PLN", amount=Decimal("0"), order_id=1
        )


# ---------------------------------------------------------------------------
# release_for_order (PR2 seam)
# ---------------------------------------------------------------------------


def test_release_restores_debited_amount(db_session, customer, make_order_id):
    _grant(db_session, customer, "100.00")
    order_id = make_order_id()
    crud_store_credit.debit(
        db_session, user_id=customer.id, currency="PLN", amount=Decimal("35.00"), order_id=order_id
    )
    released = crud_store_credit.release_for_order(db_session, order_id=order_id)
    db_session.commit()
    assert released == Decimal("35.00")
    account = crud_store_credit.get_account(db_session, user_id=customer.id, currency="PLN")
    assert account is not None
    assert account.balance == Decimal("100.00")


def test_release_is_idempotent(db_session, customer, make_order_id):
    _grant(db_session, customer, "100.00")
    order_id = make_order_id()
    crud_store_credit.debit(
        db_session, user_id=customer.id, currency="PLN", amount=Decimal("25.00"), order_id=order_id
    )
    assert crud_store_credit.release_for_order(db_session, order_id=order_id) == Decimal("25.00")
    assert crud_store_credit.release_for_order(db_session, order_id=order_id) == Decimal("0.00")
    db_session.commit()
    releases = (
        db_session.query(StoreCreditEntry)
        .filter(StoreCreditEntry.order_id == order_id, StoreCreditEntry.kind == "release")
        .count()
    )
    assert releases == 1


def test_release_no_redeem_is_noop(db_session):
    assert crud_store_credit.release_for_order(db_session, order_id=98765) == Decimal("0.00")


# ---------------------------------------------------------------------------
# ledger reads
# ---------------------------------------------------------------------------


def test_entries_for_user_newest_first_across_currencies(db_session, customer):
    _grant(db_session, customer, "10.00", currency="PLN")
    _grant(db_session, customer, "20.00", currency="USD")
    _grant(db_session, customer, "5.00", currency="PLN")
    entries = crud_store_credit.entries_for_user(db_session, user_id=customer.id)
    assert len(entries) == 3
    ids = [entry.id for entry, _ in entries]
    assert ids == sorted(ids, reverse=True)
    # Currency labels come off the joined account.
    newest_entry, newest_currency = entries[0]
    assert newest_currency == "PLN"
    assert newest_entry.amount == Decimal("5.00")
