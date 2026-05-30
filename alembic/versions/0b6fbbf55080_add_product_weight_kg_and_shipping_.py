"""add product weight_kg and shipping_rates weight_tiers

Revision ID: 0b6fbbf55080
Revises: aec862992714
Create Date: 2026-05-30 12:22:37.275411

Adds:
- ``products.weight_kg`` (Float, nullable) — admin-set physical weight in
  kilograms; nullable so existing rows don't break (the order-create
  resolver treats NULL as 0).
- ``ck_products_weight_kg_nonneg`` CHECK — defense-in-depth at the DB
  level. Added by hand because Alembic ``--autogenerate`` skips
  ``CheckConstraint`` inside ``__table_args__``.
- ``shipping_rates.weight_tiers`` (JSONB, nullable) — sibling of the
  existing ``tiers`` (subtotal-banded) JSONB column. Same shape spirit
  but bands keyed by ``max_weight_kg`` instead of ``min_subtotal``. The
  CRUD canonicalises sorted ascending on insert/update; Pydantic enforces
  per-band non-negativity + ``max_weight_kg`` uniqueness.

Non-breaking-additive: existing rates without ``weight_tiers`` continue
to resolve via the existing ``free_above → subtotal-tiers → flat``
precedence chain (the resolver short-circuits cleanly on a NULL
``weight_tiers``).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '0b6fbbf55080'
down_revision: Union[str, None] = 'aec862992714'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('products', sa.Column('weight_kg', sa.Float(), nullable=True))
    op.create_check_constraint(
        'ck_products_weight_kg_nonneg',
        'products',
        'weight_kg IS NULL OR weight_kg >= 0',
    )
    op.add_column(
        'shipping_rates',
        sa.Column('weight_tiers', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('shipping_rates', 'weight_tiers')
    op.drop_constraint('ck_products_weight_kg_nonneg', 'products', type_='check')
    op.drop_column('products', 'weight_kg')
