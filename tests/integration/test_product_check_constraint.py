"""DB-level acceptance for ``ck_products_at_least_one_price``.

The Pydantic ``ProductCreate`` validator catches the all-null case at the
API boundary, but raw inserts (bulk loads, manual scripts, ORM bypass)
must also be rejected. This test bypasses Pydantic deliberately to exercise
the DB CHECK constraint.
"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

from app.models.product import Product

pytestmark = pytest.mark.integration


def test_all_null_prices_violates_check_constraint(db_session):
    product = Product(
        name="No Prices",
        slug="no-prices-violation",
        price_usd=None,
        price_pln=None,
        price_eur=None,
    )
    db_session.add(product)
    with pytest.raises(IntegrityError) as exc_info:
        db_session.flush()
    assert "ck_products_at_least_one_price" in str(exc_info.value)
    db_session.rollback()


def test_single_currency_set_satisfies_check_constraint(db_session):
    product = Product(
        name="PLN Only",
        slug="pln-only-check-ok",
        price_usd=None,
        price_pln=42.0,
        price_eur=None,
    )
    db_session.add(product)
    db_session.flush()
    assert product.id is not None
