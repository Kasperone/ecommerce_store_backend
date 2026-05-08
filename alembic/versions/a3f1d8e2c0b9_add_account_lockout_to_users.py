"""add account lockout columns to users

Revision ID: a3f1d8e2c0b9
Revises: 37da5d2bb4e2
Create Date: 2026-05-08 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a3f1d8e2c0b9'
down_revision: Union[str, None] = '37da5d2bb4e2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'users',
        sa.Column(
            'failed_login_attempts',
            sa.Integer(),
            nullable=False,
            server_default=sa.text('0'),
        ),
    )
    op.add_column(
        'users',
        sa.Column('locked_until', sa.DateTime(), nullable=True),
    )
    # Drop the server_default once the column is populated; the model owns
    # the default going forward.
    op.alter_column('users', 'failed_login_attempts', server_default=None)


def downgrade() -> None:
    op.drop_column('users', 'locked_until')
    op.drop_column('users', 'failed_login_attempts')
