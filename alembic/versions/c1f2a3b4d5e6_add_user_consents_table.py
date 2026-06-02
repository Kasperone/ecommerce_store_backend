"""add user_consents table (append-only consent log)

Revision ID: c1f2a3b4d5e6
Revises: bca85143a9bb
Create Date: 2026-06-02 08:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c1f2a3b4d5e6'
down_revision: Union[str, None] = 'bca85143a9bb'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'user_consents',
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('consent_type', sa.String(length=100), nullable=False),
        sa.Column('granted', sa.Boolean(), nullable=False),
        sa.Column('source', sa.String(length=100), nullable=True),
        sa.Column('ip', sa.String(length=100), nullable=True),
        sa.Column('user_agent', sa.String(length=512), nullable=True),
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_user_consents_id'), 'user_consents', ['id'], unique=False)
    op.create_index(
        op.f('ix_user_consents_user_id'), 'user_consents', ['user_id'], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f('ix_user_consents_user_id'), table_name='user_consents')
    op.drop_index(op.f('ix_user_consents_id'), table_name='user_consents')
    op.drop_table('user_consents')
