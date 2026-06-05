"""add newsletter_subscribers

Revision ID: e9d201adcfbd
Revises: e69ec5842da8
Create Date: 2026-06-05 10:35:58.340506

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e9d201adcfbd'
down_revision: Union[str, None] = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('newsletter_subscribers',
    sa.Column('email', sa.String(length=320), nullable=False),
    sa.Column('is_active', sa.Boolean(), server_default='true', nullable=False),
    sa.Column('source', sa.String(length=100), nullable=True),
    sa.Column('ip', sa.String(length=100), nullable=True),
    sa.Column('user_agent', sa.String(length=512), nullable=True),
    sa.Column('unsubscribe_token', sa.String(length=128), nullable=False),
    sa.Column('confirmed_at', sa.DateTime(), nullable=True),
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_newsletter_subscribers_id'), 'newsletter_subscribers', ['id'], unique=False)
    op.create_index(op.f('ix_newsletter_subscribers_email'), 'newsletter_subscribers', ['email'], unique=True)
    op.create_index(op.f('ix_newsletter_subscribers_unsubscribe_token'), 'newsletter_subscribers', ['unsubscribe_token'], unique=True)


def downgrade() -> None:
    op.drop_index(op.f('ix_newsletter_subscribers_unsubscribe_token'), table_name='newsletter_subscribers')
    op.drop_index(op.f('ix_newsletter_subscribers_email'), table_name='newsletter_subscribers')
    op.drop_index(op.f('ix_newsletter_subscribers_id'), table_name='newsletter_subscribers')
    op.drop_table('newsletter_subscribers')
