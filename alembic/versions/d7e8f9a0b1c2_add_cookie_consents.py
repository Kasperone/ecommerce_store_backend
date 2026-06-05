"""add cookie_consents table

Revision ID: d7e8f9a0b1c2
Revises: e69ec5842da8
Create Date: 2026-06-05 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'd7e8f9a0b1c2'
down_revision: Union[str, None] = 'e9d201adcfbd'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'cookie_consents',
        sa.Column('anon_id', sa.String(length=64), nullable=True),
        sa.Column('user_id', sa.Integer(), nullable=True),
        sa.Column('categories', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('gpc', sa.Boolean(), nullable=False),
        sa.Column('policy_version', sa.String(length=50), nullable=True),
        sa.Column('ip', sa.String(length=45), nullable=True),
        sa.Column('user_agent', sa.String(length=512), nullable=True),
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        op.f('ix_cookie_consents_anon_id'),
        'cookie_consents',
        ['anon_id'],
        unique=False,
    )
    op.create_index(
        op.f('ix_cookie_consents_id'),
        'cookie_consents',
        ['id'],
        unique=False,
    )
    op.create_index(
        op.f('ix_cookie_consents_user_id'),
        'cookie_consents',
        ['user_id'],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f('ix_cookie_consents_user_id'), table_name='cookie_consents')
    op.drop_index(op.f('ix_cookie_consents_id'), table_name='cookie_consents')
    op.drop_index(op.f('ix_cookie_consents_anon_id'), table_name='cookie_consents')
    op.drop_table('cookie_consents')
