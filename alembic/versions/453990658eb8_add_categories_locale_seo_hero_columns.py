"""add categories per-locale + SEO meta + hero image columns

PR2 of the categories overhaul plan
(``ecommerce_store/.claude/notes/plans/admin/in-progress/2026-05-21-categories-overhaul.md``).

Every new column is nullable so admins can fill content in incrementally
without blocking the existing CREATE/UPDATE surface. The partial unique
index on ``slug_pl`` matches the per-locale lookup pattern: two PL slugs
must not collide, but ``NULL`` (locale not filled in) is allowed many
times. ``meta_title`` / ``meta_description`` lengths are also enforced by
the column ``String(70)`` / ``String(160)`` sizes; the CHECK constraints
are defense-in-depth (and document the limit at the DB level).

Revision ID: 453990658eb8
Revises: 712be85f37cb
Create Date: 2026-05-21 17:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "453990658eb8"
down_revision: Union[str, None] = "712be85f37cb"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Polish locale content
    op.add_column("categories", sa.Column("name_pl", sa.String(length=100), nullable=True))
    op.add_column("categories", sa.Column("slug_pl", sa.String(length=100), nullable=True))
    op.add_column("categories", sa.Column("description_pl", sa.Text(), nullable=True))

    # SEO meta (per locale)
    op.add_column("categories", sa.Column("meta_title", sa.String(length=70), nullable=True))
    op.add_column(
        "categories", sa.Column("meta_description", sa.String(length=160), nullable=True)
    )
    op.add_column("categories", sa.Column("meta_title_pl", sa.String(length=70), nullable=True))
    op.add_column(
        "categories", sa.Column("meta_description_pl", sa.String(length=160), nullable=True)
    )

    # Hero image columns (1:1 with the row, no separate table)
    op.add_column(
        "categories", sa.Column("hero_image_url", sa.String(length=500), nullable=True)
    )
    op.add_column(
        "categories", sa.Column("hero_image_alt", sa.String(length=200), nullable=True)
    )
    op.add_column("categories", sa.Column("hero_width", sa.Integer(), nullable=True))
    op.add_column("categories", sa.Column("hero_height", sa.Integer(), nullable=True))
    op.add_column("categories", sa.Column("hero_bytes", sa.Integer(), nullable=True))
    op.add_column("categories", sa.Column("hero_mime", sa.String(length=50), nullable=True))

    op.create_index(
        "uq_categories_slug_pl",
        "categories",
        ["slug_pl"],
        unique=True,
        postgresql_where=sa.text("slug_pl IS NOT NULL"),
    )
    op.create_check_constraint(
        "ck_categories_meta_title_len",
        "categories",
        "meta_title IS NULL OR char_length(meta_title) <= 70",
    )
    op.create_check_constraint(
        "ck_categories_meta_description_len",
        "categories",
        "meta_description IS NULL OR char_length(meta_description) <= 160",
    )
    op.create_check_constraint(
        "ck_categories_meta_title_pl_len",
        "categories",
        "meta_title_pl IS NULL OR char_length(meta_title_pl) <= 70",
    )
    op.create_check_constraint(
        "ck_categories_meta_description_pl_len",
        "categories",
        "meta_description_pl IS NULL OR char_length(meta_description_pl) <= 160",
    )


def downgrade() -> None:
    op.drop_constraint("ck_categories_meta_description_pl_len", "categories", type_="check")
    op.drop_constraint("ck_categories_meta_title_pl_len", "categories", type_="check")
    op.drop_constraint("ck_categories_meta_description_len", "categories", type_="check")
    op.drop_constraint("ck_categories_meta_title_len", "categories", type_="check")
    op.drop_index(
        "uq_categories_slug_pl",
        table_name="categories",
        postgresql_where=sa.text("slug_pl IS NOT NULL"),
    )
    op.drop_column("categories", "hero_mime")
    op.drop_column("categories", "hero_bytes")
    op.drop_column("categories", "hero_height")
    op.drop_column("categories", "hero_width")
    op.drop_column("categories", "hero_image_alt")
    op.drop_column("categories", "hero_image_url")
    op.drop_column("categories", "meta_description_pl")
    op.drop_column("categories", "meta_title_pl")
    op.drop_column("categories", "meta_description")
    op.drop_column("categories", "meta_title")
    op.drop_column("categories", "description_pl")
    op.drop_column("categories", "slug_pl")
    op.drop_column("categories", "name_pl")
