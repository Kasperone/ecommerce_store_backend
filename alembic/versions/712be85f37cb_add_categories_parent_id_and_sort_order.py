"""add categories.parent_id and sort_order for hierarchy + reorder

PR1 of the categories overhaul plan
(``ecommerce_store/.claude/notes/plans/admin/in-progress/2026-05-21-categories-overhaul.md``).

Both columns are nullable / default-0, so the migration is online-safe even on
a populated table — Postgres can fill the new ``sort_order`` cells in place
without a full-table rewrite. ``ON DELETE RESTRICT`` on ``parent_id`` matches
the route-layer cascade-or-reparent dialog: the DB stays the safety net while
the API surfaces the mode explicitly.

Revision ID: 712be85f37cb
Revises: e18508cdf8c5
Create Date: 2026-05-21 16:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "712be85f37cb"
down_revision: Union[str, None] = "e18508cdf8c5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "categories",
        sa.Column("parent_id", sa.Integer(), nullable=True),
    )
    op.add_column(
        "categories",
        sa.Column(
            "sort_order",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.create_foreign_key(
        "fk_categories_parent_id",
        "categories",
        "categories",
        ["parent_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index("ix_categories_parent_id", "categories", ["parent_id"], unique=False)
    op.create_index(
        "ix_categories_parent_sort",
        "categories",
        ["parent_id", "sort_order"],
        unique=False,
    )
    op.create_check_constraint(
        "ck_categories_sort_order_nonneg",
        "categories",
        "sort_order >= 0",
    )


def downgrade() -> None:
    op.drop_constraint("ck_categories_sort_order_nonneg", "categories", type_="check")
    op.drop_index("ix_categories_parent_sort", table_name="categories")
    op.drop_index("ix_categories_parent_id", table_name="categories")
    op.drop_constraint("fk_categories_parent_id", "categories", type_="foreignkey")
    op.drop_column("categories", "sort_order")
    op.drop_column("categories", "parent_id")
