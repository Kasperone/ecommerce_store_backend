"""add variant refs to order and quote lines

Variants Migration V2 (PR-B2, order side):

1. ``order_items.variant_id`` — nullable FK to ``product_variants`` with
   ON DELETE SET NULL (the order line survives variant deletion with its
   snapshots, same discipline as ``coupon_code`` / ``shipping_method_name``)
   plus the ``variant_title`` String(200) snapshot ("Red / XL").
2. ``quote_items.variant_id`` — same nullable SET NULL FK; the quote line
   keeps its snapshotted ``description`` / ``unit_price``.

No data backfill — variants are strictly additive (nullable-refs design), so
zero existing rows change. Pure DDL, safe on a live dev DB.

Revision ID: 3b7fbb1689a9
Revises: 661469d53f5a
Create Date: 2026-06-07 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '3b7fbb1689a9'
down_revision: Union[str, None] = '661469d53f5a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- 1. order_items.variant_id (SET NULL) + variant_title snapshot -----
    op.add_column('order_items', sa.Column('variant_id', sa.Integer(), nullable=True))
    op.add_column(
        'order_items', sa.Column('variant_title', sa.String(length=200), nullable=True)
    )
    op.create_index(
        op.f('ix_order_items_variant_id'), 'order_items', ['variant_id'], unique=False
    )
    op.create_foreign_key(
        'fk_order_items_variant_id_product_variants',
        'order_items',
        'product_variants',
        ['variant_id'],
        ['id'],
        ondelete='SET NULL',
    )

    # --- 2. quote_items.variant_id (SET NULL) ------------------------------
    op.add_column('quote_items', sa.Column('variant_id', sa.Integer(), nullable=True))
    op.create_index(
        op.f('ix_quote_items_variant_id'), 'quote_items', ['variant_id'], unique=False
    )
    op.create_foreign_key(
        'fk_quote_items_variant_id_product_variants',
        'quote_items',
        'product_variants',
        ['variant_id'],
        ['id'],
        ondelete='SET NULL',
    )


def downgrade() -> None:
    # quote_items
    op.drop_constraint(
        'fk_quote_items_variant_id_product_variants', 'quote_items', type_='foreignkey'
    )
    op.drop_index(op.f('ix_quote_items_variant_id'), table_name='quote_items')
    op.drop_column('quote_items', 'variant_id')

    # order_items
    op.drop_constraint(
        'fk_order_items_variant_id_product_variants', 'order_items', type_='foreignkey'
    )
    op.drop_index(op.f('ix_order_items_variant_id'), table_name='order_items')
    op.drop_column('order_items', 'variant_title')
    op.drop_column('order_items', 'variant_id')
