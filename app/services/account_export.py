"""Build the GDPR data-export payload for a single user (Art. 15/20).

Owner-scoped: the caller passes their own ``User`` (derived from the bearer
token in the route) — there is no cross-user path here. The export gathers the
user profile, their address book, their orders (with line items), and the full
consent audit trail into plain JSON-serialisable dicts.

Auth secrets are never exported: ``hashed_password``, the password-reset token
hash, and the reserved TOTP secret are stripped from the user snapshot.
"""

from typing import Any

from sqlalchemy.orm import Session

from app.crud import saved_cart as crud_saved_cart
from app.crud import store_credit as crud_store_credit
from app.crud import user_consent as crud_user_consent
from app.models.gift_card import GiftCard
from app.models.order import Order
from app.models.user import User

# Columns scrubbed from the user snapshot — credentials / security material that a
# data-subject export must never leak even to the subject themselves.
_USER_SECRET_FIELDS = frozenset(
    {
        "hashed_password",
        "password_reset_token_hash",
        "totp_secret_enc",
    }
)

# The gift-card code hash is derived security material (codes are bearer money,
# D4) — no reason to hand it out, even to the linked user.
_GIFT_CARD_SECRET_FIELDS = frozenset({"code_hash"})


def _columns_to_dict(obj: Any, *, exclude: frozenset[str] = frozenset()) -> dict[str, Any]:
    """Serialise an ORM row's mapped columns into a plain dict.

    Only real table columns are read (no relationships) so this never triggers a
    surprise lazy load. ``datetime`` values are ISO-formatted for JSON.
    """
    result: dict[str, Any] = {}
    for column in obj.__table__.columns:
        name = column.name
        if name in exclude:
            continue
        value = getattr(obj, name)
        result[name] = value.isoformat() if hasattr(value, "isoformat") else value
    return result


def build_export(db: Session, *, user: User) -> dict[str, Any]:
    """Assemble the full personal-data export for ``user``.

    Returns a dict matching ``DataExportResponse``: ``user`` profile (minus
    secrets), ``addresses``, ``orders`` (each with an ``items`` list), the
    ``consents`` audit trail (newest-first), and the ``saved_cart`` snapshot
    (the server-side abandoned-cart mirror; ``null`` when the user has none).
    """
    user_snapshot = _columns_to_dict(user, exclude=_USER_SECRET_FIELDS)

    addresses = [_columns_to_dict(addr) for addr in user.addresses]

    orders_data: list[dict[str, Any]] = []
    orders = (
        db.query(Order)
        .filter(Order.user_id == user.id)
        .order_by(Order.created_at.desc(), Order.id.desc())
        .all()
    )
    for order in orders:
        order_dict = _columns_to_dict(order)
        order_dict["items"] = [_columns_to_dict(item) for item in order.items]
        orders_data.append(order_dict)

    consents = [
        _columns_to_dict(row) for row in crud_user_consent.list_for_user(db, user_id=user.id)
    ]

    saved_cart_row = crud_saved_cart.get_by_user(db, user_id=user.id)
    saved_cart = _columns_to_dict(saved_cart_row) if saved_cart_row is not None else None

    # Stored-value data (gift-cards + store-credit program). Gift cards are
    # included when linked by user_id (the code hash — derived security
    # material — is stripped); store credit is the user's accounts + the full
    # ledger across them, newest first.
    gift_cards = [
        _columns_to_dict(card, exclude=_GIFT_CARD_SECRET_FIELDS)
        for card in db.query(GiftCard)
        .filter(GiftCard.user_id == user.id)
        .order_by(GiftCard.created_at.desc(), GiftCard.id.desc())
        .all()
    ]
    store_credit = {
        "accounts": [
            _columns_to_dict(account)
            for account in crud_store_credit.accounts_for_user(db, user_id=user.id)
        ],
        "entries": [
            {**_columns_to_dict(entry), "currency": currency}
            for entry, currency in crud_store_credit.entries_for_user(db, user_id=user.id)
        ],
    }

    return {
        "user": user_snapshot,
        "addresses": addresses,
        "orders": orders_data,
        "consents": consents,
        "saved_cart": saved_cart,
        "gift_cards": gift_cards,
        "store_credit": store_credit,
    }
