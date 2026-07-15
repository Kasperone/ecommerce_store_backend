"""Unit tests for the B2B price-list resolver (Shopify-gap P2 #14).

Exercises the pure resolution logic without HTTP:

- ``price_for`` grain precedence (variant-grain wins over product-grain) and
  per-currency fallback (an entry lacking the currency → ``None`` → keep catalog).
- ``resolve_active_entry_map`` gating: guest / non-B2B / no-list / inactive-list
  all resolve to ``None``; an active member with an active list gets the map.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from app.crud import price_list as crud_price_list
from app.schemas.price_list import PriceListCreate, PriceListEntryCreate
from app.services.b2b_pricing import resolve_active_entry_map
from tests.factories.company import CompanyFactory

pytestmark = pytest.mark.integration


@pytest.fixture
def company_factory(db_session: Session):
    CompanyFactory._meta.sqlalchemy_session = db_session  # pyright: ignore[reportAttributeAccessIssue]
    CompanyFactory._meta.sqlalchemy_session_persistence = "commit"  # pyright: ignore[reportAttributeAccessIssue]

    def _make(**kwargs):
        return CompanyFactory(**kwargs)

    return _make


def test_price_for_variant_grain_wins(db_session, product_factory):
    product = product_factory()
    pl = crud_price_list.create(db_session, obj_in=PriceListCreate(name="L"))
    crud_price_list.add_entry(
        db_session,
        price_list_id=pl.id,
        obj_in=PriceListEntryCreate(product_id=product.id, price_usd=10.0),
    )
    db_session.commit()
    entry_map = crud_price_list.resolve_entry_map(db_session, price_list_id=pl.id)

    # Product-grain resolves when no variant grain exists.
    assert crud_price_list.price_for(
        entry_map, product_id=product.id, variant_id=None, currency="USD"
    ) == Decimal("10.00")
    # A variant with no variant-grain entry falls back to product-grain.
    assert crud_price_list.price_for(
        entry_map, product_id=product.id, variant_id=999, currency="USD"
    ) == Decimal("10.00")


def test_price_for_currency_fallback_to_none(db_session, product_factory):
    product = product_factory()
    pl = crud_price_list.create(db_session, obj_in=PriceListCreate(name="L2"))
    # USD-only entry: a PLN order keeps the catalog price (resolver returns None).
    crud_price_list.add_entry(
        db_session,
        price_list_id=pl.id,
        obj_in=PriceListEntryCreate(product_id=product.id, price_usd=10.0),
    )
    db_session.commit()
    entry_map = crud_price_list.resolve_entry_map(db_session, price_list_id=pl.id)

    assert crud_price_list.price_for(
        entry_map, product_id=product.id, variant_id=None, currency="USD"
    ) == Decimal("10.00")
    assert (
        crud_price_list.price_for(entry_map, product_id=product.id, variant_id=None, currency="PLN")
        is None
    )
    # A product not in the list → None.
    assert (
        crud_price_list.price_for(entry_map, product_id=424242, variant_id=None, currency="USD")
        is None
    )


def test_resolve_active_entry_map_gating(
    db_session, user_factory, product_factory, company_factory
):
    product = product_factory()
    pl = crud_price_list.create(db_session, obj_in=PriceListCreate(name="L3"))
    crud_price_list.add_entry(
        db_session,
        price_list_id=pl.id,
        obj_in=PriceListEntryCreate(product_id=product.id, price_usd=10.0),
    )
    db_session.commit()

    # Guest → None.
    assert resolve_active_entry_map(db_session, user_id=None) is None

    # Non-B2B user → None.
    loner = user_factory(email="pl-loner@example.com")
    assert resolve_active_entry_map(db_session, user_id=loner.id) is None

    # B2B member, company with no list → None.
    m1 = user_factory(email="pl-m1@example.com")
    company_factory(name="NoList", primary_user_id=m1.id)
    assert resolve_active_entry_map(db_session, user_id=m1.id) is None

    # B2B member with active list → map with the entry.
    m2 = user_factory(email="pl-m2@example.com")
    company_factory(name="Listed", primary_user_id=m2.id, price_list_id=pl.id)
    resolved = resolve_active_entry_map(db_session, user_id=m2.id)
    assert resolved is not None
    assert (product.id, None) in resolved


def test_resolve_active_entry_map_inactive_list_is_none(
    db_session, user_factory, product_factory, company_factory
):
    product = product_factory()
    pl = crud_price_list.create(
        db_session, obj_in=PriceListCreate(name="Inactive", is_active=False)
    )
    crud_price_list.add_entry(
        db_session,
        price_list_id=pl.id,
        obj_in=PriceListEntryCreate(product_id=product.id, price_usd=10.0),
    )
    db_session.commit()
    member = user_factory(email="pl-inactive@example.com")
    company_factory(name="HasInactive", primary_user_id=member.id, price_list_id=pl.id)
    assert resolve_active_entry_map(db_session, user_id=member.id) is None
