"""Drop orphan public_id columns

The hybrid UUID architecture (integer PK + public_id UUID for API) was added
in a migration whose file is no longer present. The columns and indexes
remained in the database but no code references them. Dropping to align
schema with code.

Revision ID: b2c3d4e5f6a7
Revises: 841166999346
Create Date: 2026-05-07
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b2c3d4e5f6a7'
down_revision: Union[str, None] = '841166999346'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


TABLES = ['users', 'products', 'categories', 'orders', 'order_items', 'verification_tokens']


def upgrade() -> None:
    for table in TABLES:
        op.drop_index(f'ix_{table}_public_id', table_name=table)
        op.drop_column(table, 'public_id')


def downgrade() -> None:
    for table in TABLES:
        op.add_column(
            table,
            sa.Column('public_id', sa.dialects.postgresql.UUID(as_uuid=True),
                      nullable=False, server_default=sa.text('gen_random_uuid()')),
        )
        op.create_index(f'ix_{table}_public_id', table, ['public_id'], unique=True)
