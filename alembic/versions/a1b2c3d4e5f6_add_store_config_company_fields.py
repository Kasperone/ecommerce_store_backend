"""add store_config company/social fields

Revision ID: a1b2c3d4e5f6
Revises: e69ec5842da8
Create Date: 2026-06-05 00:00:00.000000

Extends the single-row ``store_config`` table with the company-identification +
social columns the storefront footer legally needs (EU CRD / PL): ``legal_name``,
``address_line2`` (correspondence address), ``nip``, ``regon``, ``krs``,
``phone``, and ``social_instagram`` / ``social_facebook`` / ``social_tiktok`` /
``social_youtube``.

All are ``String`` with ``server_default=''`` (never NULL), mirroring the existing
text columns. The existing id=1 row picks up the server defaults automatically, so
there is no data backfill needed.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, None] = 'f1a2b3c4d5e6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('store_config', sa.Column('legal_name', sa.String(length=255), server_default='', nullable=False))
    op.add_column('store_config', sa.Column('address_line2', sa.String(length=255), server_default='', nullable=False))
    op.add_column('store_config', sa.Column('nip', sa.String(length=64), server_default='', nullable=False))
    op.add_column('store_config', sa.Column('regon', sa.String(length=64), server_default='', nullable=False))
    op.add_column('store_config', sa.Column('krs', sa.String(length=64), server_default='', nullable=False))
    op.add_column('store_config', sa.Column('phone', sa.String(length=64), server_default='', nullable=False))
    op.add_column('store_config', sa.Column('social_instagram', sa.String(length=255), server_default='', nullable=False))
    op.add_column('store_config', sa.Column('social_facebook', sa.String(length=255), server_default='', nullable=False))
    op.add_column('store_config', sa.Column('social_tiktok', sa.String(length=255), server_default='', nullable=False))
    op.add_column('store_config', sa.Column('social_youtube', sa.String(length=255), server_default='', nullable=False))


def downgrade() -> None:
    op.drop_column('store_config', 'social_youtube')
    op.drop_column('store_config', 'social_tiktok')
    op.drop_column('store_config', 'social_facebook')
    op.drop_column('store_config', 'social_instagram')
    op.drop_column('store_config', 'phone')
    op.drop_column('store_config', 'krs')
    op.drop_column('store_config', 'regon')
    op.drop_column('store_config', 'nip')
    op.drop_column('store_config', 'address_line2')
    op.drop_column('store_config', 'legal_name')
