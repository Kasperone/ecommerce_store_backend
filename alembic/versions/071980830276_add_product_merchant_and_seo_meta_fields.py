"""add product merchant and seo meta fields

Revision ID: 071980830276
Revises: a1c2e3f40517
Create Date: 2026-06-07 10:05:57.196103

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '071980830276'
down_revision: Union[str, None] = 'a1c2e3f40517'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('products', sa.Column('brand', sa.String(length=100), nullable=True))
    op.add_column('products', sa.Column('gtin', sa.String(length=14), nullable=True))
    op.add_column('products', sa.Column('mpn', sa.String(length=70), nullable=True))
    op.add_column('products', sa.Column('condition', sa.String(length=20), server_default='new', nullable=False))
    op.add_column('products', sa.Column('meta_title', sa.String(length=70), nullable=True))
    op.add_column('products', sa.Column('meta_description', sa.String(length=160), nullable=True))
    op.add_column('products', sa.Column('meta_title_pl', sa.String(length=70), nullable=True))
    op.add_column('products', sa.Column('meta_description_pl', sa.String(length=160), nullable=True))
    # CHECK constraints — autogenerate doesn't detect these; kept in sync with
    # the model's __table_args__ (lengths mirror Category's 70/160 convention).
    op.create_check_constraint(
        'ck_products_meta_title_len', 'products',
        'meta_title IS NULL OR char_length(meta_title) <= 70',
    )
    op.create_check_constraint(
        'ck_products_meta_description_len', 'products',
        'meta_description IS NULL OR char_length(meta_description) <= 160',
    )
    op.create_check_constraint(
        'ck_products_meta_title_pl_len', 'products',
        'meta_title_pl IS NULL OR char_length(meta_title_pl) <= 70',
    )
    op.create_check_constraint(
        'ck_products_meta_description_pl_len', 'products',
        'meta_description_pl IS NULL OR char_length(meta_description_pl) <= 160',
    )
    op.create_check_constraint(
        'ck_products_condition_valid', 'products',
        "condition IN ('new', 'refurbished', 'used')",
    )


def downgrade() -> None:
    op.drop_constraint('ck_products_condition_valid', 'products', type_='check')
    op.drop_constraint('ck_products_meta_description_pl_len', 'products', type_='check')
    op.drop_constraint('ck_products_meta_title_pl_len', 'products', type_='check')
    op.drop_constraint('ck_products_meta_description_len', 'products', type_='check')
    op.drop_constraint('ck_products_meta_title_len', 'products', type_='check')
    op.drop_column('products', 'meta_description_pl')
    op.drop_column('products', 'meta_title_pl')
    op.drop_column('products', 'meta_description')
    op.drop_column('products', 'meta_title')
    op.drop_column('products', 'condition')
    op.drop_column('products', 'mpn')
    op.drop_column('products', 'gtin')
    op.drop_column('products', 'brand')
