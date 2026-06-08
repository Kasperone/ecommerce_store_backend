"""low stock default threshold

Revision ID: a633a63562e5
Revises: ce937e2a759b
Create Date: 2026-06-08 10:18:13.197720

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a633a63562e5'
down_revision: Union[str, None] = 'ce937e2a759b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Additive: NULL = no global default → today's low-stock behaviour is
    # preserved byte-for-byte (no backfill). The CHECK is hand-added — Alembic
    # autogenerate does not emit CHECK constraints.
    op.add_column('store_config', sa.Column('low_stock_default_threshold', sa.Integer(), nullable=True))
    op.create_check_constraint(
        'ck_store_config_low_stock_default_threshold_nonneg',
        'store_config',
        'low_stock_default_threshold IS NULL OR low_stock_default_threshold >= 0',
    )


def downgrade() -> None:
    op.drop_constraint(
        'ck_store_config_low_stock_default_threshold_nonneg',
        'store_config',
        type_='check',
    )
    op.drop_column('store_config', 'low_stock_default_threshold')
