"""make product prices nullable with at-least-one check

Revision ID: e18508cdf8c5
Revises: d3a1db58af77
Create Date: 2026-05-21 12:59:21.882203

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "e18508cdf8c5"
down_revision: Union[str, None] = "d3a1db58af77"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        "products",
        "price_usd",
        existing_type=sa.DOUBLE_PRECISION(precision=53),
        nullable=True,
    )
    op.alter_column(
        "products",
        "price_pln",
        existing_type=sa.DOUBLE_PRECISION(precision=53),
        nullable=True,
    )
    op.alter_column(
        "products",
        "price_eur",
        existing_type=sa.DOUBLE_PRECISION(precision=53),
        nullable=True,
    )
    op.create_check_constraint(
        "ck_products_at_least_one_price",
        "products",
        "price_usd IS NOT NULL OR price_pln IS NOT NULL OR price_eur IS NOT NULL",
    )


def downgrade() -> None:
    op.drop_constraint("ck_products_at_least_one_price", "products", type_="check")
    op.alter_column(
        "products",
        "price_eur",
        existing_type=sa.DOUBLE_PRECISION(precision=53),
        nullable=False,
    )
    op.alter_column(
        "products",
        "price_pln",
        existing_type=sa.DOUBLE_PRECISION(precision=53),
        nullable=False,
    )
    op.alter_column(
        "products",
        "price_usd",
        existing_type=sa.DOUBLE_PRECISION(precision=53),
        nullable=False,
    )
