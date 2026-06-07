"""automatic discounts + free_shipping coupon kind

Revision ID: 6e814ebe7cb5
Revises: 71b7f8d072af
Create Date: 2026-06-08 01:13:40.063685

Coupons gain a third ``discount_type`` ('free_shipping' — 13 chars, so the
column widens 8→16) and an ``is_automatic`` flag (checkout applies the single
best automatic discount with no code typed). free_shipping coupons carry
value=0, so the value CHECK is relaxed for that type only. The partial index
backs the per-checkout scan over active automatics.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '6e814ebe7cb5'
down_revision: Union[str, None] = '71b7f8d072af'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        'coupons',
        'discount_type',
        existing_type=sa.String(length=8),
        type_=sa.String(length=16),
        existing_nullable=False,
    )
    op.drop_constraint('ck_coupons_discount_type_enum', 'coupons', type_='check')
    op.create_check_constraint(
        'ck_coupons_discount_type_enum',
        'coupons',
        "discount_type IN ('percent', 'fixed', 'free_shipping')",
    )
    op.drop_constraint('ck_coupons_value_positive', 'coupons', type_='check')
    op.create_check_constraint(
        'ck_coupons_value_positive',
        'coupons',
        "value > 0 OR discount_type = 'free_shipping'",
    )
    op.add_column(
        'coupons',
        sa.Column('is_automatic', sa.Boolean(), nullable=False, server_default='false'),
    )
    op.create_index(
        'ix_coupons_automatic_active',
        'coupons',
        ['id'],
        unique=False,
        postgresql_where=sa.text('is_automatic AND is_active'),
    )


def downgrade() -> None:
    op.drop_index('ix_coupons_automatic_active', table_name='coupons')
    op.drop_column('coupons', 'is_automatic')
    # Any free_shipping rows would violate the restored constraints — remove
    # them first (downgrade is destructive for data only the upgrade allows).
    op.execute("DELETE FROM coupons WHERE discount_type = 'free_shipping'")
    op.drop_constraint('ck_coupons_value_positive', 'coupons', type_='check')
    op.create_check_constraint('ck_coupons_value_positive', 'coupons', 'value > 0')
    op.drop_constraint('ck_coupons_discount_type_enum', 'coupons', type_='check')
    op.create_check_constraint(
        'ck_coupons_discount_type_enum',
        'coupons',
        "discount_type IN ('percent', 'fixed')",
    )
    op.alter_column(
        'coupons',
        'discount_type',
        existing_type=sa.String(length=16),
        type_=sa.String(length=8),
        existing_nullable=False,
    )
