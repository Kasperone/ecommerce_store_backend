"""add brand_tagline + logo_url to store_config

Revision ID: a1c2e3f40517
Revises: d7e8f9a0b1c2
Create Date: 2026-06-06 12:00:00.000000

Adds the two brand-identity fields that make ``store_config`` the storefront's
source of truth for the wordmark and footer tagline (the brand *name* reuses the
existing ``store_name``). Both are non-null with a ``''`` server default — an
unset value is empty string, exactly like the other text fields.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "a1c2e3f40517"
down_revision: Union[str, None] = "d7e8f9a0b1c2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "store_config",
        sa.Column("brand_tagline", sa.String(length=255), server_default="", nullable=False),
    )
    op.add_column(
        "store_config",
        sa.Column("logo_url", sa.String(length=1024), server_default="", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("store_config", "logo_url")
    op.drop_column("store_config", "brand_tagline")
