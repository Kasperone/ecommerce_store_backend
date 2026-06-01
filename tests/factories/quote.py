"""Factory-boy factories for Quote + QuoteItem.

Session binding follows the same pattern as ``CompanyFactory`` — populated
dynamically by a per-test fixture. ``QuoteFactory`` defaults to a draft USD
quote for a fresh company (via ``CompanyFactory`` SubFactory) with zero totals
and no items; tests add items / set totals explicitly, or build a quote through
``crud_quote.create_with_items`` when they want server-computed totals.
"""

from __future__ import annotations

import factory
from factory.alchemy import SQLAlchemyModelFactory

from app.models.quote import Quote, QuoteItem
from tests.factories.company import CompanyFactory


class QuoteFactory(SQLAlchemyModelFactory):
    class Meta:  # pyright: ignore[reportIncompatibleVariableOverride] -- factory-boy idiom; Meta is the documented config hook
        model = Quote
        sqlalchemy_session = None  # populated by the quote_factory fixture
        sqlalchemy_session_persistence = "commit"

    company = factory.SubFactory(CompanyFactory)
    status = "draft"
    currency = "USD"
    valid_until = None
    notes = None
    subtotal = 0.0
    total = 0.0


class QuoteItemFactory(SQLAlchemyModelFactory):
    class Meta:  # pyright: ignore[reportIncompatibleVariableOverride] -- factory-boy idiom; Meta is the documented config hook
        model = QuoteItem
        sqlalchemy_session = None  # populated by the quote_factory fixture
        sqlalchemy_session_persistence = "commit"

    quote = factory.SubFactory(QuoteFactory)
    product_id = None
    description = factory.Sequence(lambda n: f"Line item {n}")
    quantity = 1
    unit_price = 10.0
    line_total = 10.0
