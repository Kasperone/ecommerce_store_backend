"""draft orders

Admin-composed orders-in-progress (Shopify-style draft orders): the
``draft_orders`` header + ``draft_order_items`` catalog lines. Drafts hold
no stock and create no orders — conversion is a later, separate endpoint.

Revision ID: b8c9d0e1f2a3
Revises: a7b8c9d0e1f2
Create Date: 2026-07-13
"""

from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'b8c9d0e1f2a3'
down_revision: Union[str, None] = 'a7b8c9d0e1f2'
branch_labels: Union[str, None] = None
depends_on: Union[str, None] = None


def upgrade() -> None:
    op.create_table(
        'draft_orders',
        sa.Column('user_id', sa.Integer(), nullable=True),
        sa.Column('guest_email', sa.String(length=255), nullable=True),
        sa.Column('status', sa.String(length=20), server_default='open', nullable=False),
        sa.Column('currency', sa.String(length=3), nullable=False),
        sa.Column('shipping_address', sa.String(length=500), nullable=True),
        sa.Column('shipping_city', sa.String(length=255), nullable=True),
        sa.Column('shipping_postal_code', sa.String(length=20), nullable=True),
        sa.Column('shipping_country', sa.String(length=100), nullable=True),
        sa.Column('shipping_method_id', sa.Integer(), nullable=True),
        sa.Column('discount', sa.Numeric(precision=12, scale=2), server_default='0', nullable=False),
        sa.Column('subtotal', sa.Numeric(precision=12, scale=2), server_default='0', nullable=False),
        sa.Column('total', sa.Numeric(precision=12, scale=2), server_default='0', nullable=False),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('converted_order_id', sa.Integer(), nullable=True),
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.CheckConstraint("status IN ('open', 'completed')", name='ck_draft_orders_status_enum'),
        sa.CheckConstraint('discount >= 0', name='ck_draft_orders_discount_non_negative'),
        sa.CheckConstraint('subtotal >= 0', name='ck_draft_orders_subtotal_non_negative'),
        sa.CheckConstraint('total >= 0', name='ck_draft_orders_total_non_negative'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(
            ['shipping_method_id'], ['shipping_methods.id'], ondelete='SET NULL'
        ),
        sa.ForeignKeyConstraint(['converted_order_id'], ['orders.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_draft_orders_id'), 'draft_orders', ['id'], unique=False)
    op.create_index(op.f('ix_draft_orders_user_id'), 'draft_orders', ['user_id'], unique=False)
    op.create_index(op.f('ix_draft_orders_status'), 'draft_orders', ['status'], unique=False)
    op.create_index(
        op.f('ix_draft_orders_converted_order_id'),
        'draft_orders',
        ['converted_order_id'],
        unique=False,
    )
    op.create_table(
        'draft_order_items',
        sa.Column('draft_order_id', sa.Integer(), nullable=False),
        sa.Column('product_id', sa.Integer(), nullable=True),
        sa.Column('variant_id', sa.Integer(), nullable=True),
        sa.Column('quantity', sa.Integer(), nullable=False),
        sa.Column('unit_price_override', sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.CheckConstraint('quantity > 0', name='ck_draft_order_items_quantity_positive'),
        sa.CheckConstraint(
            'unit_price_override >= 0',
            name='ck_draft_order_items_unit_price_override_non_negative',
        ),
        sa.ForeignKeyConstraint(['draft_order_id'], ['draft_orders.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['product_id'], ['products.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['variant_id'], ['product_variants.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_draft_order_items_id'), 'draft_order_items', ['id'], unique=False)
    op.create_index(
        op.f('ix_draft_order_items_draft_order_id'),
        'draft_order_items',
        ['draft_order_id'],
        unique=False,
    )
    op.create_index(
        op.f('ix_draft_order_items_product_id'), 'draft_order_items', ['product_id'], unique=False
    )
    op.create_index(
        op.f('ix_draft_order_items_variant_id'), 'draft_order_items', ['variant_id'], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f('ix_draft_order_items_variant_id'), table_name='draft_order_items')
    op.drop_index(op.f('ix_draft_order_items_product_id'), table_name='draft_order_items')
    op.drop_index(op.f('ix_draft_order_items_draft_order_id'), table_name='draft_order_items')
    op.drop_index(op.f('ix_draft_order_items_id'), table_name='draft_order_items')
    op.drop_table('draft_order_items')
    op.drop_index(op.f('ix_draft_orders_converted_order_id'), table_name='draft_orders')
    op.drop_index(op.f('ix_draft_orders_status'), table_name='draft_orders')
    op.drop_index(op.f('ix_draft_orders_user_id'), table_name='draft_orders')
    op.drop_index(op.f('ix_draft_orders_id'), table_name='draft_orders')
    op.drop_table('draft_orders')
