"""retire product.vat_rate, make tax_category_id NOT NULL

Phase-0 tax follow-up (U2): the transitional per-product ``vat_rate`` mirror is
dropped now that products are classified by tax category. This finishes the shape
change the categories migration (``1156c88e49a4``) began: every product carries a
category outright, so the ``category_for_product`` legacy-rate fallback is gone
from the code and its reason to exist is gone from the schema.

Order of operations matters. ``vat_rate`` must survive long enough to backfill
any still-unclassified product (one created after the categories migration but
before this one), because that legacy rate is the only evidence of what the
product should be taxed at. Backfill → enforce NOT NULL → drop the column.

Revision ID: e7f8a9b0c1d2
Revises: 1156c88e49a4
Create Date: 2026-07-22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e7f8a9b0c1d2"
down_revision: str | None = "1156c88e49a4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The store's home jurisdiction — the country whose current rate encodes the
# legacy ``vat_rate``. Matches ``settings.TAX_HOME_COUNTRY`` (default 'PL'); the
# categories migration seeded exactly these rows.
_HOME = "PL"


def upgrade() -> None:
    # 1) Classify any product still on the legacy path: match its ``vat_rate`` to
    #    the category whose in-force home-country rate equals it (the same rule
    #    the retired ``category_for_product`` applied). No match → left NULL for
    #    the default sweep below.
    op.execute(
        f"""
        UPDATE products p SET tax_category_id = (
            SELECT tc.id FROM tax_categories tc
            JOIN tax_rates tr ON tr.tax_category_id = tc.id
            WHERE tr.country_code = '{_HOME}'
              AND tr.rate_pct = p.vat_rate
              AND tr.valid_to IS NULL
            ORDER BY tc.id
            LIMIT 1
        )
        WHERE p.tax_category_id IS NULL
        """
    )
    # 2) Anything still unclassified (an odd legacy rate that matches no category)
    #    lands on the store default.
    op.execute(
        """
        UPDATE products SET tax_category_id = (
            SELECT id FROM tax_categories WHERE is_default LIMIT 1
        )
        WHERE tax_category_id IS NULL
        """
    )
    # 3) Fail loud if a default was never seeded (would otherwise leave NULLs and
    #    make the NOT NULL below abort with a less legible error).
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM products WHERE tax_category_id IS NULL) THEN
                RAISE EXCEPTION 'products.tax_category_id still NULL after backfill — '
                    'is a default tax category configured?';
            END IF;
        END $$
        """
    )
    op.alter_column("products", "tax_category_id", existing_type=sa.Integer(), nullable=False)
    op.drop_column("products", "vat_rate")


def downgrade() -> None:
    # Re-add the mirror with its original default, then reconstruct each product's
    # rate from its category's in-force home-country rate (falling back to 23 if a
    # category has no home rate configured).
    op.add_column(
        "products",
        sa.Column(
            "vat_rate",
            sa.Integer(),
            nullable=False,
            server_default="23",
        ),
    )
    op.execute(
        f"""
        UPDATE products p SET vat_rate = COALESCE((
            SELECT tr.rate_pct::int FROM tax_rates tr
            WHERE tr.tax_category_id = p.tax_category_id
              AND tr.country_code = '{_HOME}'
              AND tr.valid_to IS NULL
            ORDER BY tr.valid_from DESC
            LIMIT 1
        ), 23)
        """
    )
    op.alter_column("products", "tax_category_id", existing_type=sa.Integer(), nullable=True)
