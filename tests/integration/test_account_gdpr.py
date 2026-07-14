"""Integration tests for the GDPR endpoints on `/api/v1/me/*`.

`POST /me/data-export` returns the caller's profile + addresses + orders + consent
trail (Art. 15/20). `POST /me/account-deletion-request` soft-deletes: it stamps the
request and anonymizes PII immediately (name/phone/email + shipping/company),
deletes addresses, revokes refresh tokens, while keeping order/invoice rows
(Art. 17 / D-B-4). Strictly owner-scoped.
"""

from __future__ import annotations

import pytest

from app.core.security import create_access_token
from app.models.order import Order, OrderItem
from app.models.user_consent import UserConsent

pytestmark = pytest.mark.integration


def _headers(user) -> dict:
    token, _, _ = create_access_token(subject=str(user.id))
    return {"Authorization": f"Bearer {token}"}


def _make_order(db, user, *, product_id: int, order_number: str = "ORD-TEST-0001") -> Order:
    """Insert a minimal order + one item for ``user``."""
    order = Order(
        user_id=user.id,
        order_number=order_number,
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
    db.add(
        OrderItem(
            order_id=order.id,
            product_id=product_id,
            product_name="Widget",
            quantity=2,
            unit_price=50.0,
            total_price=100.0,
        )
    )
    db.commit()
    db.refresh(order)
    return order


def _make_address(client, user, **overrides) -> dict:
    payload = {
        "recipient_name": "Jane Doe",
        "street": "1 Main St",
        "city": "Warsaw",
        "postal_code": "00-001",
        "country": "PL",
    }
    payload.update(overrides)
    return payload


# --- data export -----------------------------------------------------------


async def test_data_export_shape_and_contents(client, db_session, user_factory, product_factory):
    product = product_factory()
    user = user_factory(
        email="export-shape@example.com",
        first_name="Anna",
        last_name="Nowak",
        phone="+48111222333",
    )
    # An address (via the public router so the CRUD defaults apply).
    await client.post(
        "/api/v1/addresses",
        json=_make_address(client, user),
        headers=_headers(user),
    )
    # A consent.
    await client.patch(
        "/api/v1/me/preferences",
        json={"consents": {"marketing_email": True}},
        headers=_headers(user),
    )
    # An order.
    _make_order(db_session, user, product_id=product.id, order_number="ORD-EXPORT-1")

    resp = await client.post("/api/v1/me/data-export", headers=_headers(user))
    assert resp.status_code == 200
    body = resp.json()

    assert set(body.keys()) == {
        "user",
        "addresses",
        "orders",
        "consents",
        "saved_cart",
        "gift_cards",
        "store_credit",
    }
    # No server-side cart snapshot for this user.
    assert body["saved_cart"] is None
    # No stored-value data for this user.
    assert body["gift_cards"] == []
    assert body["store_credit"] == {"accounts": [], "entries": []}
    assert body["user"]["email"] == "export-shape@example.com"
    assert body["user"]["first_name"] == "Anna"
    # Secrets never leak.
    assert "hashed_password" not in body["user"]
    assert "password_reset_token_hash" not in body["user"]

    assert len(body["addresses"]) == 1
    assert body["addresses"][0]["city"] == "Warsaw"

    assert len(body["orders"]) == 1
    assert body["orders"][0]["order_number"] == "ORD-EXPORT-1"
    assert len(body["orders"][0]["items"]) == 1
    assert body["orders"][0]["items"][0]["product_name"] == "Widget"

    assert len(body["consents"]) == 1
    assert body["consents"][0]["consent_type"] == "marketing_email"


async def test_data_export_owner_scoped(client, db_session, user_factory, product_factory):
    """User A's export only ever contains user A's data."""
    product = product_factory()
    user_a = user_factory(email="export-a@example.com")
    user_b = user_factory(email="export-b@example.com")
    _make_order(db_session, user_b, product_id=product.id, order_number="ORD-B-ONLY")

    resp = await client.post("/api/v1/me/data-export", headers=_headers(user_a))
    body = resp.json()
    assert body["user"]["email"] == "export-a@example.com"
    assert body["orders"] == []


async def test_data_export_requires_auth(client):
    resp = await client.post("/api/v1/me/data-export")
    assert resp.status_code == 401


async def test_data_export_includes_saved_cart(client, db_session, user_factory, product_factory):
    """The server-side cart snapshot (abandoned-cart mirror) is personal data."""
    product = product_factory()
    user = user_factory(email="export-cart@example.com")
    items = [{"product_id": product.id, "variant_id": None, "quantity": 2}]
    await client.put("/api/v1/carts/mine", json={"items": items}, headers=_headers(user))

    resp = await client.post("/api/v1/me/data-export", headers=_headers(user))
    assert resp.status_code == 200
    saved_cart = resp.json()["saved_cart"]
    assert saved_cart is not None
    assert saved_cart["user_id"] == user.id
    assert saved_cart["items"] == items


# --- account deletion ------------------------------------------------------


async def test_deletion_request_sets_flag_and_anonymizes(client, db_session, user_factory):
    user = user_factory(
        email="delete-me@example.com",
        first_name="Anna",
        last_name="Nowak",
        phone="+48111222333",
    )
    user.company_name = "Acme Sp. z o.o."
    db_session.add(user)
    db_session.commit()

    # Give them an address that must be wiped.
    await client.post(
        "/api/v1/addresses",
        json=_make_address(client, user),
        headers=_headers(user),
    )

    resp = await client.post("/api/v1/me/account-deletion-request", headers=_headers(user))
    assert resp.status_code == 200
    body = resp.json()
    assert body["deletion_requested_at"] is not None
    assert body["anonymized_at"] is not None

    db_session.refresh(user)
    assert user.deletion_requested_at is not None
    assert user.anonymized_at is not None
    assert user.email == f"deleted-user-{user.id}@anon.invalid"
    assert user.phone is None
    assert user.company_name is None
    assert user.is_active is False
    assert user.addresses == []


async def test_deletion_removes_saved_cart(client, db_session, user_factory, product_factory):
    """Anonymize drops the saved-cart snapshot (the user row is kept, so the
    FK cascade never fires — the delete is explicit in ``crud_user.anonymize``)."""
    from app.models.saved_cart import SavedCart

    product = product_factory()
    user = user_factory(email="delete-cart@example.com")
    items = [{"product_id": product.id, "variant_id": None, "quantity": 1}]
    await client.put("/api/v1/carts/mine", json={"items": items}, headers=_headers(user))
    assert db_session.query(SavedCart).filter(SavedCart.user_id == user.id).count() == 1

    resp = await client.post("/api/v1/me/account-deletion-request", headers=_headers(user))
    assert resp.status_code == 200
    assert db_session.query(SavedCart).filter(SavedCart.user_id == user.id).count() == 0


async def test_deletion_keeps_order_rows(client, db_session, user_factory, product_factory):
    """Order rows survive anonymization (tax retention), still FK'd to the user."""
    product = product_factory()
    user = user_factory(email="delete-orders@example.com")
    order = _make_order(db_session, user, product_id=product.id, order_number="ORD-KEEP-1")

    await client.post("/api/v1/me/account-deletion-request", headers=_headers(user))

    db_session.refresh(order)
    surviving = db_session.query(Order).filter(Order.id == order.id).one()
    assert surviving.user_id == user.id
    assert surviving.order_number == "ORD-KEEP-1"


async def test_deletion_revokes_refresh_tokens(client, db_session, user_factory):
    from datetime import UTC, datetime, timedelta

    from app.crud.refresh_token import refresh_token as crud_rt

    user = user_factory(email="delete-tokens@example.com")
    crud_rt.create(
        db_session,
        user_id=user.id,
        jti="jti-1",
        token="plain-token-1",
        expires_at=datetime.now(UTC) + timedelta(days=7),
    )
    assert crud_rt.list_active_for_user(db_session, user_id=user.id)

    await client.post("/api/v1/me/account-deletion-request", headers=_headers(user))

    assert crud_rt.list_active_for_user(db_session, user_id=user.id) == []


async def test_deletion_preserves_consent_audit_trail(client, db_session, user_factory):
    """Consent rows are part of the audit trail and survive (cascade not triggered
    since the user row itself is kept)."""
    user = user_factory(email="delete-consent@example.com")
    await client.patch(
        "/api/v1/me/preferences",
        json={"consents": {"marketing_email": True}},
        headers=_headers(user),
    )
    await client.post("/api/v1/me/account-deletion-request", headers=_headers(user))

    rows = db_session.query(UserConsent).filter(UserConsent.user_id == user.id).all()
    assert len(rows) == 1


async def test_deletion_requires_auth(client):
    resp = await client.post("/api/v1/me/account-deletion-request")
    assert resp.status_code == 401


# --- stored value (gift cards + store credit) --------------------------------


async def test_data_export_includes_gift_cards_and_store_credit(client, db_session, user_factory):
    """Gift cards linked by user_id and the store-credit ledger are personal
    data (Art. 15/20) — exported, minus the derived code hash."""
    from decimal import Decimal

    from app.crud import gift_card as crud_gift_card
    from app.crud import store_credit as crud_store_credit

    user = user_factory(email="export-sv@example.com")
    card, _code = crud_gift_card.issue(
        db_session,
        initial_value=Decimal("100.00"),
        currency="PLN",
        recipient_email="export-sv@example.com",
        user_id=user.id,
    )
    crud_store_credit.adjust(
        db_session, user_id=user.id, currency="EUR", amount=Decimal("25.00"), note="goodwill"
    )
    db_session.commit()

    resp = await client.post("/api/v1/me/data-export", headers=_headers(user))
    assert resp.status_code == 200
    body = resp.json()

    assert len(body["gift_cards"]) == 1
    exported_card = body["gift_cards"][0]
    assert exported_card["id"] == card.id
    assert exported_card["last4"] == card.last4
    assert "code_hash" not in exported_card  # derived security material

    accounts = body["store_credit"]["accounts"]
    assert len(accounts) == 1
    assert accounts[0]["currency"] == "EUR"
    assert float(accounts[0]["balance"]) == 25.0
    entries = body["store_credit"]["entries"]
    assert len(entries) == 1
    assert entries[0]["currency"] == "EUR"


async def test_data_export_gift_cards_owner_scoped(client, db_session, user_factory):
    """Cards belonging to someone else never leak into my export."""
    from decimal import Decimal

    from app.crud import gift_card as crud_gift_card

    me = user_factory(email="export-sv-me@example.com")
    other = user_factory(email="export-sv-other@example.com")
    crud_gift_card.issue(
        db_session, initial_value=Decimal("10.00"), currency="PLN", user_id=other.id
    )
    db_session.commit()

    body = (await client.post("/api/v1/me/data-export", headers=_headers(me))).json()
    assert body["gift_cards"] == []


async def test_deletion_scrubs_gift_card_recipient_email(client, db_session, user_factory):
    """Anonymize scrubs recipient_email on cards linked by user_id AND on
    unlinked cards issued to the subject's address; the card rows themselves
    are financial records and survive (like orders)."""
    from decimal import Decimal

    from app.crud import gift_card as crud_gift_card
    from app.models.gift_card import GiftCard

    user = user_factory(email="delete-gc@example.com")
    linked, _ = crud_gift_card.issue(
        db_session,
        initial_value=Decimal("50.00"),
        currency="PLN",
        recipient_email="delete-gc@example.com",
        user_id=user.id,
    )
    unlinked, _ = crud_gift_card.issue(
        db_session,
        initial_value=Decimal("30.00"),
        currency="PLN",
        recipient_email="delete-gc@example.com",
    )
    other, _ = crud_gift_card.issue(
        db_session,
        initial_value=Decimal("10.00"),
        currency="PLN",
        recipient_email="someone-else@example.com",
    )
    db_session.commit()

    resp = await client.post("/api/v1/me/account-deletion-request", headers=_headers(user))
    assert resp.status_code == 200

    for card in (linked, unlinked, other):
        db_session.refresh(card)
    assert linked.recipient_email is None
    assert unlinked.recipient_email is None
    assert other.recipient_email == "someone-else@example.com"  # untouched
    # Rows kept, balances intact, linkage preserved (financial record).
    assert db_session.query(GiftCard).filter(GiftCard.id == linked.id).count() == 1
    assert linked.user_id == user.id
    assert linked.balance == Decimal("50.00")


async def test_deletion_keeps_store_credit_ledger(client, db_session, user_factory):
    """Store-credit accounts + entries carry no PII columns — kept, like orders."""
    from decimal import Decimal

    from app.crud import store_credit as crud_store_credit
    from app.models.store_credit import StoreCreditAccount, StoreCreditEntry

    user = user_factory(email="delete-sc@example.com")
    crud_store_credit.adjust(db_session, user_id=user.id, currency="PLN", amount=Decimal("40.00"))
    db_session.commit()

    await client.post("/api/v1/me/account-deletion-request", headers=_headers(user))

    account = (
        db_session.query(StoreCreditAccount).filter(StoreCreditAccount.user_id == user.id).one()
    )
    assert account.balance == Decimal("40.00")
    assert (
        db_session.query(StoreCreditEntry).filter(StoreCreditEntry.account_id == account.id).count()
        == 1
    )
