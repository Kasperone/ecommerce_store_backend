"""drop products images JSON, add product_images table

Replaces the legacy ``products.images`` JSON list-of-URLs with a row-per-image
relational model carrying alt-text, sort order, primary flag, dimensions, and
magic-byte-validated MIME. PR0b of the admin-catalog foundation plan
(``ecommerce_store/.claude/notes/plans/admin/pending/2026-05-18-admin-catalog-foundation.md``).

The downgrade re-creates the JSON column with a ``'[]'`` server default so it
is reversible against a populated database — existing image URLs in the JSON
column are not preserved across the upgrade (clean break; no production data
to migrate).

Revision ID: d3a1db58af77
Revises: 6706dda745e8
Create Date: 2026-05-18 22:48:51.362903

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'd3a1db58af77'
down_revision: Union[str, None] = '6706dda745e8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'product_images',
        sa.Column('product_id', sa.Integer(), nullable=False),
        sa.Column('url', sa.String(length=500), nullable=False),
        sa.Column('alt_text', sa.String(length=200), nullable=True),
        sa.Column('sort_order', sa.Integer(), nullable=False),
        sa.Column('is_primary', sa.Boolean(), nullable=False),
        sa.Column('width', sa.Integer(), nullable=False),
        sa.Column('height', sa.Integer(), nullable=False),
        sa.Column('bytes', sa.Integer(), nullable=False),
        sa.Column('mime', sa.String(length=50), nullable=False),
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['product_id'], ['products.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_product_images_id'), 'product_images', ['id'], unique=False)
    op.create_index(
        op.f('ix_product_images_product_id'),
        'product_images',
        ['product_id'],
        unique=False,
    )
    op.create_index(
        'uq_product_images_primary_per_product',
        'product_images',
        ['product_id'],
        unique=True,
        postgresql_where=sa.text('is_primary'),
    )
    op.drop_column('products', 'images')


def downgrade() -> None:
    op.add_column(
        'products',
        sa.Column(
            'images',
            postgresql.JSON(astext_type=sa.Text()),
            autoincrement=False,
            nullable=False,
            # Existing rows must satisfy NOT NULL after the column comes back.
            server_default=sa.text("'[]'::json"),
        ),
    )
    op.drop_index(
        'uq_product_images_primary_per_product',
        table_name='product_images',
        postgresql_where=sa.text('is_primary'),
    )
    op.drop_index(op.f('ix_product_images_product_id'), table_name='product_images')
    op.drop_index(op.f('ix_product_images_id'), table_name='product_images')
    op.drop_table('product_images')
