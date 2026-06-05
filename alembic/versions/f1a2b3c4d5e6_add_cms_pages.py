"""add cms pages and page_versions tables

Revision ID: f1a2b3c4d5e6
Revises: e69ec5842da8
Create Date: 2026-06-05 12:00:00.000000

CMS page-builder foundation: ``pages`` (per-locale composable pages with a JSONB
section layout, draft/published split + version pointer + SEO) and
``page_versions`` (append-only immutable layout snapshots). ``pages`` and
``page_versions`` reference each other (page→current-version, version→page), so
the ``pages.published_version_id`` FK is added after both tables exist.

str-enums (type/locale/status) are ``String`` + CHECK constraints (no PG enum,
mirrors ``banners.placement``); layouts are ``JSONB``.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'f1a2b3c4d5e6'
down_revision: Union[str, None] = 'e69ec5842da8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'pages',
        sa.Column('slug', sa.String(length=255), nullable=False),
        sa.Column('type', sa.String(length=16), nullable=False),
        sa.Column('locale', sa.String(length=5), nullable=False),
        sa.Column('status', sa.String(length=16), server_default='draft', nullable=False),
        sa.Column('draft_layout', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('published_layout', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('published_version_id', sa.Integer(), nullable=True),
        sa.Column('scheduled_at', sa.DateTime(), nullable=True),
        sa.Column('unpublish_at', sa.DateTime(), nullable=True),
        sa.Column('seo_title', sa.String(length=255), nullable=True),
        sa.Column('seo_description', sa.String(length=500), nullable=True),
        sa.Column('seo_image_url', sa.String(length=2048), nullable=True),
        sa.Column('created_by', sa.Integer(), nullable=True),
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.CheckConstraint("type IN ('home', 'standard', 'landing')", name='ck_pages_type_enum'),
        sa.CheckConstraint("locale IN ('en', 'pl')", name='ck_pages_locale_enum'),
        sa.CheckConstraint(
            "status IN ('draft', 'published', 'scheduled')", name='ck_pages_status_enum'
        ),
        sa.ForeignKeyConstraint(['created_by'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('slug', 'locale', name='uq_pages_slug_locale'),
    )
    op.create_index(op.f('ix_pages_id'), 'pages', ['id'], unique=False)
    op.create_index(op.f('ix_pages_slug'), 'pages', ['slug'], unique=False)

    op.create_table(
        'page_versions',
        sa.Column('page_id', sa.Integer(), nullable=False),
        sa.Column('version_no', sa.Integer(), nullable=False),
        sa.Column('layout', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('label', sa.String(length=255), nullable=True),
        sa.Column('created_by', sa.Integer(), nullable=True),
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['page_id'], ['pages.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['created_by'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_page_versions_id'), 'page_versions', ['id'], unique=False)
    op.create_index(
        op.f('ix_page_versions_page_id'), 'page_versions', ['page_id'], unique=False
    )

    # Circular FK added last (page → its currently-live version).
    op.create_foreign_key(
        'fk_pages_published_version_id',
        'pages',
        'page_versions',
        ['published_version_id'],
        ['id'],
        ondelete='SET NULL',
    )


def downgrade() -> None:
    op.drop_constraint('fk_pages_published_version_id', 'pages', type_='foreignkey')
    op.drop_index(op.f('ix_page_versions_page_id'), table_name='page_versions')
    op.drop_index(op.f('ix_page_versions_id'), table_name='page_versions')
    op.drop_table('page_versions')
    op.drop_index(op.f('ix_pages_slug'), table_name='pages')
    op.drop_index(op.f('ix_pages_id'), table_name='pages')
    op.drop_table('pages')
