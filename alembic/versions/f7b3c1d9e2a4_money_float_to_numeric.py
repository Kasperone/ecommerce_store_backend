"""Migrate all money columns from double precision (Float) to Numeric(12, 2).

Money stored as binary float drifts (``0.1 + 0.2 != 0.3``) and accumulates error
across subtotal → discount → tax → total — which on a Polish faktura (a legal
document) can make ``net + vat != gross`` by a grosz. ``numeric(12, 2)`` is exact
base-10 fixed point; SQLAlchemy returns ``Decimal`` for it, and the app now does
all money math in ``Decimal`` (see ``app/core/money.py``).

Postgres casts ``double precision -> numeric(12, 2)`` in place (rounding to 2
decimals), so existing data is preserved. Physical-quantity floats (weight_kg,
dimensions, shipping ``dim_divisor``) are deliberately left as ``double
precision`` — they are not money.

Revision ID: f7b3c1d9e2a4
Revises: f6a1b2c3d4e5
Create Date: 2026-06-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f7b3c1d9e2a4"
down_revision: str | None = "f6a1b2c3d4e5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# (table, column) for every monetary column.
MONEY_COLUMNS: list[tuple[str, str]] = [
    ("products", "price_usd"),
    ("products", "price_pln"),
    ("products", "price_eur"),
    ("product_variants", "price_usd"),
    ("product_variants", "price_pln"),
    ("product_variants", "price_eur"),
    ("orders", "subtotal"),
    ("orders", "tax"),
    ("orders", "shipping"),
    ("orders", "discount"),
    ("orders", "total"),
    ("order_items", "unit_price"),
    ("order_items", "total_price"),
    ("coupons", "value"),
    ("coupons", "min_order_amount"),
    ("refunds", "amount"),
    ("shipping_rates", "amount"),
    ("shipping_rates", "free_above_subtotal"),
    ("price_history", "price"),
    ("quotes", "subtotal"),
    ("quotes", "total"),
    ("quote_items", "unit_price"),
    ("quote_items", "line_total"),
]


def upgrade() -> None:
    for table, column in MONEY_COLUMNS:
        op.alter_column(
            table,
            column,
            type_=sa.Numeric(12, 2),
            existing_type=sa.Float(),
            postgresql_using=f"{column}::numeric(12,2)",
        )


def downgrade() -> None:
    for table, column in MONEY_COLUMNS:
        op.alter_column(
            table,
            column,
            type_=sa.Float(),
            existing_type=sa.Numeric(12, 2),
            postgresql_using=f"{column}::double precision",
        )
