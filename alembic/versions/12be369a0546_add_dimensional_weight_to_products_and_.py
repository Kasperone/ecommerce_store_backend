"""add dimensional weight to products and zones

Revision ID: 12be369a0546
Revises: 0b6fbbf55080
Create Date: 2026-05-30 15:08:29.077371

Adds the columns and CHECK constraints that drive volumetric (dimensional)
shipping-weight pricing — the courier convention of charging
``max(actual_kg, volumetric_kg)`` where ``volumetric_kg = (l × w × h) / divisor``.

Columns:
- ``products.length_cm`` / ``width_cm`` / ``height_cm`` (Float, nullable):
  admin-set physical dimensions in centimetres. Nullable so existing rows
  don't break (the order-create resolver treats "any dim NULL" as "no
  volumetric contribution" so a missing dimension contributes 0 to the
  cart's total volume — same NULL-as-0 forgiveness as ``weight_kg``).
- ``shipping_zones.dim_divisor`` (Float, nullable): cm³/kg divisor for the
  zone. PL/EU ground ≈ 5000, international air ≈ 6000. NULL = no
  volumetric pricing for this zone (resolver skips the volumetric branch,
  bills actual weight only — backwards-compatible default).

CHECK constraints (added by hand because Alembic ``--autogenerate`` skips
``CheckConstraint`` inside ``__table_args__``, per the lesson banked in
the ``ck_products_weight_kg_nonneg`` migration):
- ``ck_products_length_cm_nonneg`` / ``_width_cm_`` / ``_height_cm_``: each
  ``cm IS NULL OR cm >= 0``.
- ``ck_shipping_zones_dim_divisor_positive``:
  ``dim_divisor IS NULL OR dim_divisor > 0`` — divisors must be strictly
  positive (a zero divisor would divide-by-zero in the resolver).

Non-breaking-additive: existing zones without ``dim_divisor`` keep
resolving with actual-weight-only pricing. Admin opts in zone-by-zone by
setting a divisor; the seed sets 5000 on the PL zone going forward but
leaves existing PL rows untouched on rerun.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '12be369a0546'
down_revision: Union[str, None] = '0b6fbbf55080'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('products', sa.Column('length_cm', sa.Float(), nullable=True))
    op.add_column('products', sa.Column('width_cm', sa.Float(), nullable=True))
    op.add_column('products', sa.Column('height_cm', sa.Float(), nullable=True))
    op.add_column('shipping_zones', sa.Column('dim_divisor', sa.Float(), nullable=True))

    # Hand-added CHECK constraints — autogenerate skips __table_args__ CHECKs.
    op.create_check_constraint(
        'ck_products_length_cm_nonneg',
        'products',
        'length_cm IS NULL OR length_cm >= 0',
    )
    op.create_check_constraint(
        'ck_products_width_cm_nonneg',
        'products',
        'width_cm IS NULL OR width_cm >= 0',
    )
    op.create_check_constraint(
        'ck_products_height_cm_nonneg',
        'products',
        'height_cm IS NULL OR height_cm >= 0',
    )
    op.create_check_constraint(
        'ck_shipping_zones_dim_divisor_positive',
        'shipping_zones',
        'dim_divisor IS NULL OR dim_divisor > 0',
    )


def downgrade() -> None:
    op.drop_constraint(
        'ck_shipping_zones_dim_divisor_positive', 'shipping_zones', type_='check'
    )
    op.drop_constraint('ck_products_height_cm_nonneg', 'products', type_='check')
    op.drop_constraint('ck_products_width_cm_nonneg', 'products', type_='check')
    op.drop_constraint('ck_products_length_cm_nonneg', 'products', type_='check')
    op.drop_column('shipping_zones', 'dim_divisor')
    op.drop_column('products', 'height_cm')
    op.drop_column('products', 'width_cm')
    op.drop_column('products', 'length_cm')
