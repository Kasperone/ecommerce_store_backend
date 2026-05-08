"""add password reset and password_changed_at to users

Revision ID: 37da5d2bb4e2
Revises: b2c3d4e5f6a7
Create Date: 2026-05-08 09:17:10.697075

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '37da5d2bb4e2'
down_revision: Union[str, None] = 'b2c3d4e5f6a7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'users',
        sa.Column('password_reset_token_hash', sa.String(length=64), nullable=True),
    )
    op.add_column(
        'users',
        sa.Column('password_reset_expires_at', sa.DateTime(), nullable=True),
    )
    op.add_column(
        'users',
        sa.Column('password_changed_at', sa.DateTime(), nullable=True),
    )
    op.create_index(
        op.f('ix_users_password_reset_token_hash'),
        'users',
        ['password_reset_token_hash'],
        unique=False,
    )
    # Backfill: existing accounts have no recorded change, treat created_at as
    # the baseline so any post-migration password rotation moves it forward.
    op.execute("UPDATE users SET password_changed_at = created_at WHERE password_changed_at IS NULL")


def downgrade() -> None:
    op.drop_index(op.f('ix_users_password_reset_token_hash'), table_name='users')
    op.drop_column('users', 'password_changed_at')
    op.drop_column('users', 'password_reset_expires_at')
    op.drop_column('users', 'password_reset_token_hash')
