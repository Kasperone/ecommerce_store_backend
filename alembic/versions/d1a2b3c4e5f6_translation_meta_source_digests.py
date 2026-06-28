"""Add translation_meta source-digest sidecar for outdated detection

Revision ID: d1a2b3c4e5f6
Revises: b31ac594c42b
Create Date: 2026-06-28 03:05:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd1a2b3c4e5f6'
down_revision: Union[str, None] = 'b31ac594c42b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'translation_meta',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.Column('resource_type', sa.String(length=40), nullable=False),
        sa.Column('resource_id', sa.Integer(), nullable=False),
        sa.Column('field_key', sa.String(length=40), nullable=False),
        sa.Column('locale', sa.String(length=10), nullable=False),
        sa.Column('source_digest', sa.String(length=64), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'resource_type',
            'resource_id',
            'field_key',
            'locale',
            name='uq_translation_meta_resource_field_locale',
        ),
    )
    op.create_index(op.f('ix_translation_meta_id'), 'translation_meta', ['id'], unique=False)
    op.create_index(
        op.f('ix_translation_meta_resource_type'),
        'translation_meta',
        ['resource_type'],
        unique=False,
    )
    op.create_index(
        op.f('ix_translation_meta_resource_id'),
        'translation_meta',
        ['resource_id'],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f('ix_translation_meta_resource_id'), table_name='translation_meta')
    op.drop_index(op.f('ix_translation_meta_resource_type'), table_name='translation_meta')
    op.drop_index(op.f('ix_translation_meta_id'), table_name='translation_meta')
    op.drop_table('translation_meta')
