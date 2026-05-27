"""add product name_pl + description_pl columns

BE-A of the storefront search plan
(``ecommerce_store/.claude/notes/plans/search/pending/2026-05-27-search-meilisearch.md``).

Products had no Polish content (only categories did), so a Polish shopper
searching "słuchawki" found nothing. Both columns are nullable — existing rows
backfill to NULL and the storefront falls back to the EN field at render time.
Mirrors the ``categories.name_pl`` / ``description_pl`` columns. No ``slug_pl``:
products keep their single slug (URL i18n is out of scope).

Revision ID: e11b73891adc
Revises: 5b03d4cc1a57
Create Date: 2026-05-27 12:55:00.545669

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "e11b73891adc"
down_revision: Union[str, None] = "5b03d4cc1a57"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("products", sa.Column("name_pl", sa.String(length=200), nullable=True))
    op.add_column("products", sa.Column("description_pl", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("products", "description_pl")
    op.drop_column("products", "name_pl")
