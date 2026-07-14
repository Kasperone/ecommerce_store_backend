"""Unit tests for ``crud/gift_card.py`` (DB-backed, no HTTP).

Covers issue (card + opening ledger row, hash-at-rest), lookup by plaintext
variants, admin adjust (ledger + negative-balance guard), usability checks
(status/expiry/balance/currency machine codes), the PR2 seams ``debit`` /
``release_for_order`` (FOR-UPDATE semantics, order_id keying, idempotent
release), and exact Decimal math.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.crud.gift_card import GiftCardError, hash_code
from app.crud.gift_card import gift_card as crud_gift_card
from app.models.gift_card import GiftCard, GiftCardEntry, GiftCardStatus
from app.models.order import Order

pytestmark = pytest.mark.integration


def _utcnow_naive() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


@pytest.fixture
def make_order_id(db_session, user_factory):
    """Callable minting real order ids (ledger order_id is a real FK)."""
    counter = {"n": 0}

    def _make() -> int:
        counter["n"] += 1
        user = user_factory(email=f"gc-order-{counter['n']}@example.com")
        order = Order(
            user_id=user.id,
            order_number=f"GC-TEST-{user.id}-{counter['n']}",
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


def _issue(
    db,
    *,
    initial_value: Decimal = Decimal("100.00"),
    currency: str = "PLN",
    expires_at: datetime | None = None,
    recipient_email: str | None = None,
) -> tuple[GiftCard, str]:
    card, code = crud_gift_card.issue(
        db,
        initial_value=initial_value,
        currency=currency,
        expires_at=expires_at,
        recipient_email=recipient_email,
    )
    db.commit()
    return card, code


# ---------------------------------------------------------------------------
# issue
# ---------------------------------------------------------------------------


def test_issue_creates_card_with_opening_ledger_entry(db_session):
    card, _code = _issue(db_session, initial_value=Decimal("150.50"), currency="USD")
    assert card.balance == Decimal("150.50")
    assert card.initial_value == Decimal("150.50")
    assert card.status == GiftCardStatus.ACTIVE.value
    assert len(card.entries) == 1
    entry = card.entries[0]
    assert entry.kind == "issue"
    assert entry.amount == Decimal("150.50")
    assert entry.balance_after == Decimal("150.50")


def test_issue_stores_hash_and_last4_never_plaintext(db_session):
    card, code = _issue(db_session)
    normalized = code.replace("-", "")
    assert card.code_hash == hash_code(code)
    assert card.last4 == normalized[-4:]
    assert len(card.code_hash) == 64  # sha256 hex, not the code
    assert normalized not in card.code_hash


def test_issue_returns_dash_formatted_code(db_session):
    _, code = _issue(db_session)
    parts = code.split("-")
    assert len(parts) == 4
    assert all(len(p) == 4 for p in parts)


def test_get_by_code_accepts_presentation_variants(db_session):
    card, code = _issue(db_session)
    for variant in (code, code.lower(), code.replace("-", " "), code.replace("-", "")):
        found = crud_gift_card.get_by_code(db_session, code=variant)
        assert found is not None
        assert found.id == card.id
    assert crud_gift_card.get_by_code(db_session, code="AAAA-BBBB-CCCC-DDDD") is None


def test_issue_quantizes_value(db_session):
    card, _ = _issue(db_session, initial_value=Decimal("99.999"))
    assert card.balance == Decimal("100.00")


# ---------------------------------------------------------------------------
# adjust
# ---------------------------------------------------------------------------


def test_adjust_positive_and_negative_update_balance_and_ledger(db_session):
    card, _ = _issue(db_session, initial_value=Decimal("50.00"))
    crud_gift_card.adjust(db_session, gift_card_id=card.id, amount=Decimal("25.00"))
    db_session.commit()
    db_session.refresh(card)
    assert card.balance == Decimal("75.00")

    crud_gift_card.adjust(
        db_session, gift_card_id=card.id, amount=Decimal("-30.00"), note="clawback"
    )
    db_session.commit()
    db_session.refresh(card)
    assert card.balance == Decimal("45.00")

    kinds = [e.kind for e in card.entries]
    assert kinds == ["issue", "adjust", "adjust"]
    last = card.entries[-1]
    assert last.amount == Decimal("-30.00")
    assert last.balance_after == Decimal("45.00")
    assert last.note == "clawback"


def test_adjust_below_zero_raises_and_leaves_balance(db_session):
    card, _ = _issue(db_session, initial_value=Decimal("20.00"))
    with pytest.raises(GiftCardError) as exc_info:
        crud_gift_card.adjust(db_session, gift_card_id=card.id, amount=Decimal("-20.01"))
    assert exc_info.value.code == "gift_card_balance_negative"
    db_session.rollback()
    db_session.refresh(card)
    assert card.balance == Decimal("20.00")
    assert len(card.entries) == 1  # no ledger row for the refused adjust


def test_adjust_unknown_card_raises_not_found(db_session):
    with pytest.raises(GiftCardError) as exc_info:
        crud_gift_card.adjust(db_session, gift_card_id=999999, amount=Decimal("5.00"))
    assert exc_info.value.code == "gift_card_not_found"


# ---------------------------------------------------------------------------
# check_usable
# ---------------------------------------------------------------------------


def test_check_usable_passes_for_active_funded_card(db_session):
    card, _ = _issue(db_session, currency="EUR")
    crud_gift_card.check_usable(card=card, currency="EUR")  # no raise


@pytest.mark.parametrize(
    ("mutate", "currency", "expected_code"),
    [
        (lambda c: setattr(c, "status", "disabled"), "PLN", "gift_card_disabled"),
        (
            lambda c: setattr(c, "expires_at", _utcnow_naive() - timedelta(days=1)),
            "PLN",
            "gift_card_expired",
        ),
        (lambda c: setattr(c, "balance", Decimal("0.00")), "PLN", "gift_card_depleted"),
        (lambda c: None, "USD", "gift_card_currency_mismatch"),
    ],
)
def test_check_usable_machine_codes(db_session, mutate, currency, expected_code):
    card, _ = _issue(db_session, currency="PLN")
    mutate(card)
    with pytest.raises(GiftCardError) as exc_info:
        crud_gift_card.check_usable(card=card, currency=currency)
    assert exc_info.value.code == expected_code


def test_check_usable_future_expiry_still_valid(db_session):
    card, _ = _issue(db_session, expires_at=_utcnow_naive() + timedelta(days=30))
    crud_gift_card.check_usable(card=card, currency="PLN")  # no raise


# ---------------------------------------------------------------------------
# debit (PR2 seam)
# ---------------------------------------------------------------------------


def test_debit_writes_redeem_entry_keyed_by_order(db_session, make_order_id):
    card, _ = _issue(db_session, initial_value=Decimal("100.00"))
    order_id = make_order_id()
    crud_gift_card.debit(
        db_session,
        gift_card_id=card.id,
        amount=Decimal("60.00"),
        currency="PLN",
        order_id=order_id,
    )
    db_session.commit()
    db_session.refresh(card)
    assert card.balance == Decimal("40.00")
    redeem = card.entries[-1]
    assert redeem.kind == "redeem"
    assert redeem.amount == Decimal("-60.00")
    assert redeem.balance_after == Decimal("40.00")
    assert redeem.order_id == order_id


def test_debit_over_balance_raises_depleted(db_session):
    card, _ = _issue(db_session, initial_value=Decimal("10.00"))
    with pytest.raises(GiftCardError) as exc_info:
        crud_gift_card.debit(
            db_session,
            gift_card_id=card.id,
            amount=Decimal("10.01"),
            currency="PLN",
            order_id=1,
        )
    assert exc_info.value.code == "gift_card_depleted"


def test_debit_revalidates_usability_under_lock(db_session):
    card, _ = _issue(db_session, currency="PLN")
    card.status = "disabled"
    db_session.flush()
    with pytest.raises(GiftCardError) as exc_info:
        crud_gift_card.debit(
            db_session, gift_card_id=card.id, amount=Decimal("1.00"), currency="PLN", order_id=1
        )
    assert exc_info.value.code == "gift_card_disabled"


def test_debit_currency_mismatch_under_lock(db_session):
    card, _ = _issue(db_session, currency="PLN")
    with pytest.raises(GiftCardError) as exc_info:
        crud_gift_card.debit(
            db_session, gift_card_id=card.id, amount=Decimal("1.00"), currency="EUR", order_id=1
        )
    assert exc_info.value.code == "gift_card_currency_mismatch"


def test_debit_rejects_non_positive_amount(db_session):
    card, _ = _issue(db_session)
    with pytest.raises(ValueError, match="positive"):
        crud_gift_card.debit(
            db_session, gift_card_id=card.id, amount=Decimal("0.00"), currency="PLN", order_id=1
        )


# ---------------------------------------------------------------------------
# release_for_order (PR2 seam)
# ---------------------------------------------------------------------------


def test_release_restores_debited_amount(db_session, make_order_id):
    card, _ = _issue(db_session, initial_value=Decimal("100.00"))
    order_id = make_order_id()
    crud_gift_card.debit(
        db_session, gift_card_id=card.id, amount=Decimal("70.00"), currency="PLN", order_id=order_id
    )
    released = crud_gift_card.release_for_order(db_session, order_id=order_id)
    db_session.commit()
    db_session.refresh(card)
    assert released == Decimal("70.00")
    assert card.balance == Decimal("100.00")
    release = card.entries[-1]
    assert release.kind == "release"
    assert release.amount == Decimal("70.00")
    assert release.order_id == order_id


def test_release_is_idempotent(db_session, make_order_id):
    card, _ = _issue(db_session, initial_value=Decimal("100.00"))
    order_id = make_order_id()
    crud_gift_card.debit(
        db_session, gift_card_id=card.id, amount=Decimal("30.00"), currency="PLN", order_id=order_id
    )
    assert crud_gift_card.release_for_order(db_session, order_id=order_id) == Decimal("30.00")
    assert crud_gift_card.release_for_order(db_session, order_id=order_id) == Decimal("0.00")
    db_session.commit()
    db_session.refresh(card)
    assert card.balance == Decimal("100.00")
    # Exactly one release row despite the double call.
    kinds = [e.kind for e in card.entries]
    assert kinds.count("release") == 1


def test_release_no_redeem_is_noop(db_session):
    assert crud_gift_card.release_for_order(db_session, order_id=424242) == Decimal("0.00")
    assert db_session.query(GiftCardEntry).filter(GiftCardEntry.order_id == 424242).count() == 0


def test_release_goes_through_on_disabled_card(db_session, make_order_id):
    # Cancelling an order must never strand the customer's money.
    card, _ = _issue(db_session, initial_value=Decimal("50.00"))
    order_id = make_order_id()
    crud_gift_card.debit(
        db_session, gift_card_id=card.id, amount=Decimal("50.00"), currency="PLN", order_id=order_id
    )
    card.status = "disabled"
    db_session.flush()
    assert crud_gift_card.release_for_order(db_session, order_id=order_id) == Decimal("50.00")
    db_session.refresh(card)
    assert card.balance == Decimal("50.00")


# ---------------------------------------------------------------------------
# filters
# ---------------------------------------------------------------------------


def test_get_multi_filters(db_session):
    a, _ = _issue(db_session, currency="PLN", recipient_email="anna@example.com")
    b, _ = _issue(db_session, currency="USD")
    b.status = "disabled"
    db_session.commit()

    ids = {c.id for c in crud_gift_card.get_multi(db_session, status="disabled")}
    assert b.id in ids and a.id not in ids

    ids = {c.id for c in crud_gift_card.get_multi(db_session, currency="PLN")}
    assert a.id in ids and b.id not in ids

    ids = {c.id for c in crud_gift_card.get_multi(db_session, q="anna@")}
    assert ids == {a.id}

    ids = {c.id for c in crud_gift_card.get_multi(db_session, q=a.last4.lower())}
    assert a.id in ids

    assert crud_gift_card.count(db_session, q="anna@") == 1
