"""Tax categories and per-country rates.

**Why a category and not a rate.** ``product.vat_rate: int`` stored *the answer
for Poland* having discarded *the question*: a book is 5% in PL, 7% in DE, 0% in
the UK and 4% in ES, so the literal ``5`` cannot be reprojected onto another
country. Recovering the question later means a human re-classifying every SKU by
hand. Storing the **category** and resolving ``(category, country, date) → rate``
keeps the question.

For a Poland-only store the rate table holds one row per category at ``PL`` and
returns exactly the answers ``product.vat_rate`` gave — this is a shape change
that buys optionality, not a behaviour change.

``valid_from`` / ``valid_to`` additionally fix historical correctness: when a
rate moves, reprinting an old faktura must use the rate that applied *then*.
"""

from datetime import date
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import BaseModel

# Category codes seeded by the initial migration. Referenced by the backfill and
# by tests; new codes are added by admins, so this is a starting set, not an enum.
STANDARD = "STANDARD"
REDUCED_8 = "REDUCED_8"
REDUCED_5 = "REDUCED_5"
ZERO = "ZERO"


class TaxCategory(BaseModel):
    """A *kind* of goods for tax purposes — "standard", "books", "foodstuffs".

    ``code`` is the stable identifier (referenced by imports/exports and the
    backfill); ``name`` is the admin-facing label.
    """

    __tablename__ = "tax_categories"
    __table_args__ = (
        # At most one default. A partial unique index rather than application
        # logic, so two defaults cannot exist even briefly.
        Index(
            "uq_tax_categories_single_default",
            "is_default",
            unique=True,
            postgresql_where=text("is_default"),
        ),
    )

    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(100))
    # Per-locale label, matching the store's ``*_pl`` translation convention.
    name_pl: Mapped[str | None] = mapped_column(String(100))
    # The category assigned to products that specify none. Exactly one row is
    # the default (enforced by a partial unique index created in the migration).
    is_default: Mapped[bool] = mapped_column(default=False, server_default="false")

    rates: Mapped[list["TaxRate"]] = relationship(
        back_populates="category", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<TaxCategory {self.code}>"


class TaxRate(BaseModel):
    """The rate a category attracts in one country over one validity window.

    ``rate_pct`` is ``Numeric(5,2)`` rather than an integer because several EU
    members use fractional rates (FR 2.1% / 5.5%, IE 13.5%, …). Poland's are all
    whole numbers, so nothing downstream sees a fraction today — and
    ``build_order_items`` refuses to snapshot a fractional rate rather than
    truncating one silently (``order_items.vat_rate`` is still an ``Integer``).

    Validity windows are half-open: ``valid_from`` inclusive, ``valid_to``
    exclusive, NULL meaning "still in force". Overlaps are resolved by taking the
    latest ``valid_from`` that covers the date, so resolution is deterministic
    even if an admin leaves a stale window open.
    """

    __tablename__ = "tax_rates"
    __table_args__ = (
        CheckConstraint("country_code ~ '^[A-Z]{2}$'", name="ck_tax_rates_country_iso2"),
        CheckConstraint("rate_pct >= 0 AND rate_pct <= 100", name="ck_tax_rates_rate_range"),
        CheckConstraint(
            "valid_to IS NULL OR valid_to > valid_from", name="ck_tax_rates_valid_window"
        ),
        UniqueConstraint(
            "tax_category_id",
            "country_code",
            "valid_from",
            name="uq_tax_rates_category_country_from",
        ),
    )

    tax_category_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("tax_categories.id", ondelete="CASCADE"), index=True
    )
    country_code: Mapped[str] = mapped_column(String(2), index=True)
    rate_pct: Mapped[Decimal] = mapped_column(Numeric(5, 2))
    valid_from: Mapped[date] = mapped_column(Date)
    valid_to: Mapped[date | None] = mapped_column(Date)

    category: Mapped[TaxCategory] = relationship(back_populates="rates")

    def __repr__(self) -> str:
        return f"<TaxRate cat#{self.tax_category_id} {self.country_code} {self.rate_pct}%>"
