"""add users.deletion_requested_at + anonymized_at (GDPR soft-delete)

Revision ID: d2e3f4a5b6c7
Revises: c1f2a3b4d5e6
Create Date: 2026-06-02 08:05:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd2e3f4a5b6c7'
down_revision: Union[str, None] = 'c1f2a3b4d5e6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('users', sa.Column('deletion_requested_at', sa.DateTime(), nullable=True))
    op.add_column('users', sa.Column('anonymized_at', sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column('users', 'anonymized_at')
    op.drop_column('users', 'deletion_requested_at')
