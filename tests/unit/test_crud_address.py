"""Unit tests for ``app.crud.address`` (DB-backed, no HTTP).

Covers the two invariants the CRUD layer guarantees in-transaction: first-address
auto-default, and default-flag mutual exclusion on both create and update. Owner
gating is exercised via ``get_owned``.
"""

from __future__ import annotations

import pytest

from app.crud import address as crud_address
from app.schemas.address import AddressCreate, AddressUpdate

pytestmark = pytest.mark.integration


def _create(**overrides) -> AddressCreate:
    base: dict[str, object] = {
        "recipient_name": "Jane Doe",
        "street": "1 Main St",
        "city": "Warsaw",
        "postal_code": "00-001",
        "country": "PL",
    }
    base.update(overrides)
    return AddressCreate(**base)  # pyright: ignore[reportArgumentType] -- dict-splat into a Pydantic model; values validated at runtime


def test_first_address_auto_both_defaults(db_session, user_factory):
    user = user_factory(email="crud-first@example.com")
    addr = crud_address.create(
        db_session,
        user_id=user.id,
        obj_in=_create(is_default_shipping=False, is_default_billing=False),
    )
    db_session.commit()
    assert addr.is_default_shipping is True
    assert addr.is_default_billing is True


def test_second_address_keeps_requested_flags(db_session, user_factory):
    user = user_factory(email="crud-second@example.com")
    crud_address.create(db_session, user_id=user.id, obj_in=_create(city="A"))
    db_session.commit()
    second = crud_address.create(
        db_session,
        user_id=user.id,
        obj_in=_create(city="B", is_default_shipping=False, is_default_billing=False),
    )
    db_session.commit()
    assert second.is_default_shipping is False
    assert second.is_default_billing is False


def test_create_new_default_clears_prior(db_session, user_factory):
    user = user_factory(email="crud-mx-create@example.com")
    first = crud_address.create(db_session, user_id=user.id, obj_in=_create(city="A"))
    db_session.commit()
    second = crud_address.create(
        db_session,
        user_id=user.id,
        obj_in=_create(city="B", is_default_shipping=True, is_default_billing=True),
    )
    db_session.commit()
    db_session.refresh(first)
    assert second.is_default_shipping is True
    assert second.is_default_billing is True
    assert first.is_default_shipping is False
    assert first.is_default_billing is False


def test_update_promote_shipping_clears_only_shipping(db_session, user_factory):
    """Promoting shipping must not touch the prior billing default."""
    user = user_factory(email="crud-mx-update@example.com")
    first = crud_address.create(db_session, user_id=user.id, obj_in=_create(city="A"))
    db_session.commit()
    second = crud_address.create(db_session, user_id=user.id, obj_in=_create(city="B"))
    db_session.commit()

    crud_address.update(
        db_session,
        address=second,
        obj_in=AddressUpdate(is_default_shipping=True),
    )
    db_session.commit()
    db_session.refresh(first)
    assert second.is_default_shipping is True
    assert first.is_default_shipping is False
    # Billing default still on the first row.
    assert first.is_default_billing is True
    assert second.is_default_billing is False


def test_update_partial_leaves_other_fields(db_session, user_factory):
    user = user_factory(email="crud-partial@example.com")
    addr = crud_address.create(db_session, user_id=user.id, obj_in=_create(label="Home"))
    db_session.commit()
    crud_address.update(db_session, address=addr, obj_in=AddressUpdate(label="Office"))
    db_session.commit()
    assert addr.label == "Office"
    assert addr.city == "Warsaw"


def test_get_owned_cross_user_returns_none(db_session, user_factory):
    user_a = user_factory(email="crud-own-a@example.com")
    user_b = user_factory(email="crud-own-b@example.com")
    addr = crud_address.create(db_session, user_id=user_a.id, obj_in=_create())
    db_session.commit()

    assert crud_address.get_owned(db_session, id=addr.id, user_id=user_a.id) is not None
    assert crud_address.get_owned(db_session, id=addr.id, user_id=user_b.id) is None


def test_get_multi_orders_default_first_then_newest(db_session, user_factory):
    user = user_factory(email="crud-order@example.com")
    crud_address.create(db_session, user_id=user.id, obj_in=_create(city="First"))
    crud_address.create(db_session, user_id=user.id, obj_in=_create(city="Second"))
    crud_address.create(db_session, user_id=user.id, obj_in=_create(city="Third"))
    db_session.commit()

    rows = crud_address.get_multi_for_user(db_session, user_id=user.id)
    cities = [r.city for r in rows]
    assert cities == ["First", "Third", "Second"]
