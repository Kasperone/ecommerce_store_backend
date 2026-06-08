"""google product category for shopping feed

Revision ID: f6a1b2c3d4e5
Revises: 85d9d5df29b7
Create Date: 2026-06-08 14:30:00.000000

Adds the Google Merchant Center taxonomy id used by the Google Shopping feed
(Pillar E):

* ``categories.google_product_category`` — nullable ``String(20)``; the Google
  product-category taxonomy id (e.g. "166"). Inherited by every product in the
  category.
* ``products.google_product_category`` — nullable ``String(20)``; an optional
  per-product override of the category's value.

Both nullable with no backfill — only the feed reads them, and an unset value
simply omits the ``g:google_product_category`` attribute for that item.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "f6a1b2c3d4e5"
down_revision: Union[str, None] = "85d9d5df29b7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "categories",
        sa.Column("google_product_category", sa.String(length=20), nullable=True),
    )
    op.add_column(
        "products",
        sa.Column("google_product_category", sa.String(length=20), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("products", "google_product_category")
    op.drop_column("categories", "google_product_category")
