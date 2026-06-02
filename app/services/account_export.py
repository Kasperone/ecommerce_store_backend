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

from app.crud import user_consent as crud_user_consent
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
    secrets), ``addresses``, ``orders`` (each with an ``items`` list), and the
    ``consents`` audit trail (newest-first).
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

    return {
        "user": user_snapshot,
        "addresses": addresses,
        "orders": orders_data,
        "consents": consents,
    }
