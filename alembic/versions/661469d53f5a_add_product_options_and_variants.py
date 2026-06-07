"""add product options and variants

Variants Migration V1 (PR-B1, catalog side):

1. New tables ``product_options`` / ``product_option_values`` /
   ``product_variants`` / ``product_variant_option_values`` — the Shopify-style
   option/variant model (≤3 options per product, per-variant price/SKU/image/
   stock, DB-unique ``option_signature`` per combination).
2. Nullable ``variant_id`` refs on ``stock_levels`` (CASCADE),
   ``inventory_movements`` (SET NULL — the ledger survives variant deletion),
   and ``price_history`` (CASCADE; per-variant Omnibus trail).
3. The ``stock_levels`` unique constraint is **replaced** by a three-column one
   with ``postgresql_nulls_not_distinct=True`` (PG16) so two NULL-variant rows
   for the same (product, warehouse) still collide. Hand-written — autogenerate
   does not emit NULLS NOT DISTINCT.

No data backfill: variants are strictly additive (nullable-refs design), so
zero existing rows change. Pure DDL, safe on a live dev DB.

Revision ID: 661469d53f5a
Revises: dea805323477
Create Date: 2026-06-07 23:18:51.561459

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '661469d53f5a'
down_revision: Union[str, None] = 'dea805323477'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- 1. New option/variant tables -------------------------------------
    op.create_table(
        'product_options',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.Column('product_id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=50), nullable=False),
        sa.Column('name_pl', sa.String(length=50), nullable=True),
        sa.Column('position', sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(['product_id'], ['products.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('product_id', 'name', name='uq_product_options_product_name'),
        # DEFERRABLE so a position swap within one transaction doesn't trip
        # the constraint mid-flush (reorder in the matrix PUT).
        sa.UniqueConstraint(
            'product_id',
            'position',
            name='uq_product_options_product_position',
            deferrable=True,
            initially='DEFERRED',
        ),
        sa.CheckConstraint(
            'position >= 0 AND position <= 2', name='ck_product_options_max_three'
        ),
    )
    op.create_index(op.f('ix_product_options_id'), 'product_options', ['id'], unique=False)
    op.create_index(
        op.f('ix_product_options_product_id'), 'product_options', ['product_id'], unique=False
    )

    op.create_table(
        'product_option_values',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.Column('option_id', sa.Integer(), nullable=False),
        sa.Column('value', sa.String(length=80), nullable=False),
        sa.Column('value_pl', sa.String(length=80), nullable=True),
        sa.Column('position', sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(['option_id'], ['product_options.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('option_id', 'value', name='uq_product_option_values_option_value'),
    )
    op.create_index(
        op.f('ix_product_option_values_id'), 'product_option_values', ['id'], unique=False
    )
    op.create_index(
        op.f('ix_product_option_values_option_id'),
        'product_option_values',
        ['option_id'],
        unique=False,
    )

    op.create_table(
        'product_variants',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.Column('product_id', sa.Integer(), nullable=False),
        sa.Column('title', sa.String(length=200), nullable=False),
        sa.Column('title_pl', sa.String(length=200), nullable=True),
        sa.Column('sku', sa.String(length=100), nullable=True),
        sa.Column('price_usd', sa.Float(), nullable=True),
        sa.Column('price_pln', sa.Float(), nullable=True),
        sa.Column('price_eur', sa.Float(), nullable=True),
        sa.Column('stock', sa.Integer(), nullable=False),
        sa.Column('image_id', sa.Integer(), nullable=True),
        sa.Column('position', sa.Integer(), nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=False),
        sa.Column('option_signature', sa.String(length=64), nullable=False),
        sa.ForeignKeyConstraint(['product_id'], ['products.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['image_id'], ['product_images.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'product_id', 'option_signature', name='uq_product_variants_product_signature'
        ),
    )
    op.create_index(op.f('ix_product_variants_id'), 'product_variants', ['id'], unique=False)
    op.create_index(
        op.f('ix_product_variants_product_id'), 'product_variants', ['product_id'], unique=False
    )
    op.create_index(op.f('ix_product_variants_sku'), 'product_variants', ['sku'], unique=True)

    op.create_table(
        'product_variant_option_values',
        sa.Column('variant_id', sa.Integer(), nullable=False),
        sa.Column('option_value_id', sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(['variant_id'], ['product_variants.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(
            ['option_value_id'], ['product_option_values.id'], ondelete='CASCADE'
        ),
        sa.PrimaryKeyConstraint('variant_id', 'option_value_id'),
    )

    # --- 2. stock_levels.variant_id + the NULLS NOT DISTINCT swap ----------
    op.add_column('stock_levels', sa.Column('variant_id', sa.Integer(), nullable=True))
    op.create_index(
        op.f('ix_stock_levels_variant_id'), 'stock_levels', ['variant_id'], unique=False
    )
    op.create_foreign_key(
        'fk_stock_levels_variant_id_product_variants',
        'stock_levels',
        'product_variants',
        ['variant_id'],
        ['id'],
        ondelete='CASCADE',
    )
    op.drop_constraint('uq_stock_levels_product_warehouse', 'stock_levels', type_='unique')
    # Hand-written: autogenerate will NOT emit postgresql_nulls_not_distinct.
    # Without it, two NULL-variant rows per (product, warehouse) are insertable
    # and silently corrupt the unallocated-default-stock synthesis.
    op.create_unique_constraint(
        'uq_stock_levels_product_warehouse_variant',
        'stock_levels',
        ['product_id', 'warehouse_id', 'variant_id'],
        postgresql_nulls_not_distinct=True,
    )

    # --- 3. inventory_movements.variant_id (SET NULL) ----------------------
    op.add_column('inventory_movements', sa.Column('variant_id', sa.Integer(), nullable=True))
    op.create_index(
        op.f('ix_inventory_movements_variant_id'),
        'inventory_movements',
        ['variant_id'],
        unique=False,
    )
    op.create_foreign_key(
        'fk_inventory_movements_variant_id_product_variants',
        'inventory_movements',
        'product_variants',
        ['variant_id'],
        ['id'],
        ondelete='SET NULL',
    )

    # --- 4. price_history.variant_id + extended lookup index ---------------
    op.add_column('price_history', sa.Column('variant_id', sa.Integer(), nullable=True))
    op.create_index(
        op.f('ix_price_history_variant_id'), 'price_history', ['variant_id'], unique=False
    )
    op.create_foreign_key(
        'fk_price_history_variant_id_product_variants',
        'price_history',
        'product_variants',
        ['variant_id'],
        ['id'],
        ondelete='CASCADE',
    )
    op.drop_index('ix_price_history_lookup', table_name='price_history')
    op.create_index(
        'ix_price_history_lookup',
        'price_history',
        ['product_id', 'variant_id', 'currency', 'effective_from'],
        unique=False,
    )


def downgrade() -> None:
    # price_history
    op.drop_index('ix_price_history_lookup', table_name='price_history')
    op.create_index(
        'ix_price_history_lookup',
        'price_history',
        ['product_id', 'currency', 'effective_from'],
        unique=False,
    )
    op.drop_constraint(
        'fk_price_history_variant_id_product_variants', 'price_history', type_='foreignkey'
    )
    op.drop_index(op.f('ix_price_history_variant_id'), table_name='price_history')
    op.drop_column('price_history', 'variant_id')

    # inventory_movements
    op.drop_constraint(
        'fk_inventory_movements_variant_id_product_variants',
        'inventory_movements',
        type_='foreignkey',
    )
    op.drop_index(op.f('ix_inventory_movements_variant_id'), table_name='inventory_movements')
    op.drop_column('inventory_movements', 'variant_id')

    # stock_levels (restore the old two-column unique constraint)
    op.drop_constraint(
        'uq_stock_levels_product_warehouse_variant', 'stock_levels', type_='unique'
    )
    op.create_unique_constraint(
        'uq_stock_levels_product_warehouse', 'stock_levels', ['product_id', 'warehouse_id']
    )
    op.drop_constraint(
        'fk_stock_levels_variant_id_product_variants', 'stock_levels', type_='foreignkey'
    )
    op.drop_index(op.f('ix_stock_levels_variant_id'), table_name='stock_levels')
    op.drop_column('stock_levels', 'variant_id')

    # option/variant tables (children first)
    op.drop_table('product_variant_option_values')
    op.drop_index(op.f('ix_product_variants_sku'), table_name='product_variants')
    op.drop_index(op.f('ix_product_variants_product_id'), table_name='product_variants')
    op.drop_index(op.f('ix_product_variants_id'), table_name='product_variants')
    op.drop_table('product_variants')
    op.drop_index(op.f('ix_product_option_values_option_id'), table_name='product_option_values')
    op.drop_index(op.f('ix_product_option_values_id'), table_name='product_option_values')
    op.drop_table('product_option_values')
    op.drop_index(op.f('ix_product_options_product_id'), table_name='product_options')
    op.drop_index(op.f('ix_product_options_id'), table_name='product_options')
    op.drop_table('product_options')
