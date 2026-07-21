"""Rate resolution: category, country, date.

The behaviours worth pinning here are the ones whose failure is *silent*: a
missing rate must raise rather than resolve to 0%, and a rate change must not
retroactively rewrite what an old order was taxed at.
"""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from app.models.product import Product
from app.models.tax import TaxCategory, TaxRate
from app.services.tax import (
    TaxRateNotFoundError,
    category_for_product,
    get_default_category,
    resolve_rate,
    resolve_rate_int,
)

pytestmark = pytest.mark.integration


def _category(db: Session, code: str, *, default: bool = False) -> TaxCategory:
    category = TaxCategory(code=code, name=code, is_default=default)
    db.add(category)
    db.flush()
    return category


def _rate(
    db: Session,
    category: TaxCategory,
    country: str,
    pct: str,
    valid_from: date,
    valid_to: date | None = None,
) -> TaxRate:
    rate = TaxRate(
        tax_category_id=category.id,
        country_code=country,
        rate_pct=Decimal(pct),
        valid_from=valid_from,
        valid_to=valid_to,
    )
    db.add(rate)
    db.flush()
    return rate


class TestResolveRate:
    def test_resolves_the_rate_for_the_country(self, db_session: Session) -> None:
        cat = _category(db_session, "BOOKS_T1")
        _rate(db_session, cat, "PL", "5", date(2011, 1, 1))
        _rate(db_session, cat, "DE", "7", date(2011, 1, 1))

        assert resolve_rate(
            db_session, category=cat, country_code="PL", on_date=date(2026, 7, 21)
        ) == Decimal("5.00")
        assert resolve_rate(
            db_session, category=cat, country_code="DE", on_date=date(2026, 7, 21)
        ) == Decimal("7.00")

    def test_missing_rate_raises_rather_than_returning_zero(self, db_session: Session) -> None:
        """A silent 0% would under-collect VAT, which is the seller's liability
        and cannot be billed back to the customer afterwards."""
        cat = _category(db_session, "BOOKS_T2")
        _rate(db_session, cat, "PL", "5", date(2011, 1, 1))

        with pytest.raises(TaxRateNotFoundError):
            resolve_rate(db_session, category=cat, country_code="FR", on_date=date(2026, 7, 21))

    def test_window_is_half_open_and_history_is_preserved(self, db_session: Session) -> None:
        """Reprinting an old faktura must use the rate that applied *then*."""
        cat = _category(db_session, "STD_T3")
        _rate(db_session, cat, "PL", "22", date(2000, 1, 1), date(2011, 1, 1))
        _rate(db_session, cat, "PL", "23", date(2011, 1, 1))

        # valid_to is exclusive, valid_from inclusive — the boundary day belongs
        # to the new rate, and there is no gap or overlap between them.
        assert resolve_rate(
            db_session, category=cat, country_code="PL", on_date=date(2010, 12, 31)
        ) == Decimal("22.00")
        assert resolve_rate(
            db_session, category=cat, country_code="PL", on_date=date(2011, 1, 1)
        ) == Decimal("23.00")

    def test_date_before_any_window_raises(self, db_session: Session) -> None:
        cat = _category(db_session, "STD_T4")
        _rate(db_session, cat, "PL", "23", date(2011, 1, 1))

        with pytest.raises(TaxRateNotFoundError):
            resolve_rate(db_session, category=cat, country_code="PL", on_date=date(2005, 6, 1))

    def test_overlapping_windows_resolve_to_the_latest_valid_from(
        self, db_session: Session
    ) -> None:
        """Deterministic rather than arbitrary: "most recently configured wins"."""
        cat = _category(db_session, "STD_T5")
        _rate(db_session, cat, "PL", "23", date(2011, 1, 1))
        _rate(db_session, cat, "PL", "21", date(2026, 1, 1))

        assert resolve_rate(
            db_session, category=cat, country_code="PL", on_date=date(2026, 7, 21)
        ) == Decimal("21.00")


class TestResolveRateInt:
    def test_whole_rates_narrow_to_int(self, db_session: Session) -> None:
        cat = _category(db_session, "STD_T6")
        _rate(db_session, cat, "PL", "23", date(2011, 1, 1))

        assert resolve_rate_int(db_session, category=cat, country_code="PL") == 23

    def test_fractional_rate_refuses_to_truncate(self, db_session: Session) -> None:
        """``order_items.vat_rate`` is an integer column; FR 2.1% / IE 13.5%
        exist. Truncating silently would mis-state VAT on the faktura, so the
        snapshot column must be widened deliberately instead."""
        cat = _category(db_session, "REDUCED_T7")
        _rate(db_session, cat, "FR", "2.1", date(2011, 1, 1))

        with pytest.raises(ValueError, match="fractional"):
            resolve_rate_int(db_session, category=cat, country_code="FR")


class TestDefaultCategory:
    def test_returns_the_seeded_default(self, db_session: Session) -> None:
        """The migration seeds STANDARD as the default."""
        assert get_default_category(db_session).code == "STANDARD"


class TestCategoryForProduct:
    """The transition path, where a product may carry a category, a legacy
    ``vat_rate``, or both."""

    def test_explicit_category_wins(self, db_session: Session) -> None:
        cat = _category(db_session, "BOOKS_T8")
        _rate(db_session, cat, "PL", "5", date(2011, 1, 1))
        product = Product(name="Book", slug="book-t8", price_pln=Decimal("50.00"), vat_rate=23)
        product.tax_category = cat
        db_session.add(product)
        db_session.flush()

        assert category_for_product(db_session, product, country_code="PL").code == "BOOKS_T8"

    def test_unclassified_product_matches_its_legacy_rate_not_the_default(
        self, db_session: Session
    ) -> None:
        """The regression this guard exists for: a product created after the
        categories migration has a NULL category. Falling back to the default
        would tax an 8% product at 23% — wrong, silent, and only visible on the
        faktura."""
        product = Product(name="Reduced", slug="reduced-t9", price_pln=Decimal("50.00"), vat_rate=8)
        db_session.add(product)
        db_session.flush()

        resolved = category_for_product(db_session, product, country_code="PL")
        assert resolved.code == "REDUCED_8"
        assert resolve_rate_int(db_session, category=resolved, country_code="PL") == 8

    def test_unmatchable_rate_falls_back_to_default(self, db_session: Session) -> None:
        product = Product(name="Odd", slug="odd-t10", price_pln=Decimal("50.00"), vat_rate=17)
        db_session.add(product)
        db_session.flush()

        assert category_for_product(db_session, product, country_code="PL").code == "STANDARD"
