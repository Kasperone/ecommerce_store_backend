"""store_config: add email_from_name

Dedicated sender display name for transactional emails (boilerplate brand
program B1). Blank means "fall back to store_name, then the env
EMAIL_FROM_NAME" — resolution lives in app/services/brand.py.

Revision ID: e2f3a4b5c6d7
Revises: d1a2b3c4e5f6
Create Date: 2026-07-13
"""

from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'e2f3a4b5c6d7'
down_revision: Union[str, None] = 'd1a2b3c4e5f6'
branch_labels: Union[str, None] = None
depends_on: Union[str, None] = None


def upgrade() -> None:
    op.add_column(
        'store_config',
        sa.Column('email_from_name', sa.String(length=255), server_default='', nullable=False),
    )


def downgrade() -> None:
    op.drop_column('store_config', 'email_from_name')
