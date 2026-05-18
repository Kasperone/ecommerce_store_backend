"""add admin_audit_events table

Revision ID: 6706dda745e8
Revises: 9fe7e45d1e55
Create Date: 2026-05-18 22:03:23.143801

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = '6706dda745e8'
down_revision: Union[str, None] = '9fe7e45d1e55'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'admin_audit_events',
        sa.Column('actor_id', sa.Integer(), nullable=True),
        sa.Column('action', sa.String(length=64), nullable=False),
        sa.Column('resource_type', sa.String(length=64), nullable=False),
        sa.Column('resource_id', sa.Integer(), nullable=True),
        sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('ip', sa.String(length=45), nullable=True),
        sa.Column('user_agent', sa.String(length=500), nullable=True),
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['actor_id'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        op.f('ix_admin_audit_events_actor_id'),
        'admin_audit_events',
        ['actor_id'],
        unique=False,
    )
    op.create_index(
        op.f('ix_admin_audit_events_id'),
        'admin_audit_events',
        ['id'],
        unique=False,
    )
    op.create_index(
        'ix_admin_audit_events_resource',
        'admin_audit_events',
        ['resource_type', 'resource_id', sa.literal_column('created_at DESC')],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index('ix_admin_audit_events_resource', table_name='admin_audit_events')
    op.drop_index(op.f('ix_admin_audit_events_id'), table_name='admin_audit_events')
    op.drop_index(op.f('ix_admin_audit_events_actor_id'), table_name='admin_audit_events')
    op.drop_table('admin_audit_events')
