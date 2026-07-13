"""store_config: add primary_color

Storefront primary brand color as '#rrggbb' (boilerplate brand program B4).
Blank means the built-in default theme; the frontend generates the full
Nuxt UI primary ramp from this value at render time.

Revision ID: a7b8c9d0e1f2
Revises: e2f3a4b5c6d7
Create Date: 2026-07-13
"""

from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'a7b8c9d0e1f2'
down_revision: Union[str, None] = 'e2f3a4b5c6d7'
branch_labels: Union[str, None] = None
depends_on: Union[str, None] = None


def upgrade() -> None:
    op.add_column(
        'store_config',
        sa.Column('primary_color', sa.String(length=7), server_default='', nullable=False),
    )


def downgrade() -> None:
    op.drop_column('store_config', 'primary_color')
