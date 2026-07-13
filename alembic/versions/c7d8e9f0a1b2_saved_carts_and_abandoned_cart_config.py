"""saved carts and abandoned cart config

Abandoned-cart recovery v1: the ``saved_carts`` snapshot table (one row per
authed user, ids+quantities JSONB, opaque recovery token) plus the two
``store_config`` knobs (``abandoned_cart_enabled`` — ships false —
and ``abandoned_cart_threshold_hours``).

Revision ID: c7d8e9f0a1b2
Revises: b8c9d0e1f2a3
Create Date: 2026-07-14
"""

from typing import Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c7d8e9f0a1b2"
down_revision: Union[str, None] = "b8c9d0e1f2a3"
branch_labels: Union[str, None] = None
depends_on: Union[str, None] = None


def upgrade() -> None:
    op.create_table(
        "saved_carts",
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("items", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("recovery_token", sa.String(length=64), nullable=True),
        sa.Column("recovery_email_sent_at", sa.DateTime(), nullable=True),
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_saved_carts_id"), "saved_carts", ["id"], unique=False)
    op.create_index(op.f("ix_saved_carts_user_id"), "saved_carts", ["user_id"], unique=True)
    op.create_index(
        op.f("ix_saved_carts_recovery_token"), "saved_carts", ["recovery_token"], unique=True
    )

    op.add_column(
        "store_config",
        sa.Column("abandoned_cart_enabled", sa.Boolean(), server_default="false", nullable=False),
    )
    op.add_column(
        "store_config",
        sa.Column(
            "abandoned_cart_threshold_hours", sa.Integer(), server_default="24", nullable=False
        ),
    )
    op.create_check_constraint(
        "ck_store_config_abandoned_cart_threshold_hours_min",
        "store_config",
        "abandoned_cart_threshold_hours >= 1",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_store_config_abandoned_cart_threshold_hours_min", "store_config", type_="check"
    )
    op.drop_column("store_config", "abandoned_cart_threshold_hours")
    op.drop_column("store_config", "abandoned_cart_enabled")
    op.drop_index(op.f("ix_saved_carts_recovery_token"), table_name="saved_carts")
    op.drop_index(op.f("ix_saved_carts_user_id"), table_name="saved_carts")
    op.drop_index(op.f("ix_saved_carts_id"), table_name="saved_carts")
    op.drop_table("saved_carts")
