"""add staff capabilities (is_owner + capabilities)

Back-office maturity A1: granular staff permissions. Adds two columns to
``users`` layered over the unchanged ``UserRole`` enum:

- ``is_owner`` (bool, server_default false) — an owner implicitly holds every
  capability.
- ``capabilities`` (text[]/ARRAY(String), server_default '{}') — explicit
  capability grants for a scoped staffer.

Data migration: every existing ``role='ADMIN'`` row is backfilled to
``is_owner=True`` so no current admin loses access when the write routes start
enforcing ``require_capability``. Additive; downgrade drops both columns.

Revision ID: e1f2a3b4c5d6
Revises: a4ef27ffe275
Create Date: 2026-07-15 09:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e1f2a3b4c5d6'
down_revision: Union[str, None] = 'a4ef27ffe275'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'users',
        sa.Column(
            'is_owner',
            sa.Boolean(),
            nullable=False,
            server_default=sa.text('false'),
        ),
    )
    op.add_column(
        'users',
        sa.Column(
            'capabilities',
            sa.ARRAY(sa.String()),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
    )
    # Backfill: existing admins become owners (implicitly all-capabilities) so
    # current behavior is byte-identical until a scoped staffer is created.
    op.execute("UPDATE users SET is_owner = true WHERE role = 'ADMIN'")


def downgrade() -> None:
    op.drop_column('users', 'capabilities')
    op.drop_column('users', 'is_owner')
