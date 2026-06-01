"""add quote convert marker + company primary contact

Adds the two columns that back quote→order conversion (wave 3, task QC):

- ``quotes.converted_order_id`` — nullable FK → ``orders.id``, ON DELETE SET NULL.
  The conversion marker + double-convert guard (set to the created order's id).
- ``companies.primary_user_id`` — nullable FK → ``users.id``, ON DELETE SET NULL.
  The company's designated primary contact customer; a converted order is owned
  by this real user (correct emails / history / invoices), not the admin.

Both FK constraints are named EXPLICITLY so the downgrade's ``drop_constraint``
works — Alembic autogenerate emits ``None`` for unnamed constraints which breaks
the drop (see ``5718ef59bc7c``).

Revision ID: 87d98e961941
Revises: 5718ef59bc7c
Create Date: 2026-06-01

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '87d98e961941'
down_revision: Union[str, None] = '5718ef59bc7c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('quotes', sa.Column('converted_order_id', sa.Integer(), nullable=True))
    op.create_index(
        op.f('ix_quotes_converted_order_id'), 'quotes', ['converted_order_id'], unique=False
    )
    op.create_foreign_key(
        'fk_quotes_converted_order_id_orders',
        'quotes',
        'orders',
        ['converted_order_id'],
        ['id'],
        ondelete='SET NULL',
    )

    op.add_column('companies', sa.Column('primary_user_id', sa.Integer(), nullable=True))
    op.create_index(
        op.f('ix_companies_primary_user_id'), 'companies', ['primary_user_id'], unique=False
    )
    op.create_foreign_key(
        'fk_companies_primary_user_id_users',
        'companies',
        'users',
        ['primary_user_id'],
        ['id'],
        ondelete='SET NULL',
    )


def downgrade() -> None:
    op.drop_constraint('fk_companies_primary_user_id_users', 'companies', type_='foreignkey')
    op.drop_index(op.f('ix_companies_primary_user_id'), table_name='companies')
    op.drop_column('companies', 'primary_user_id')

    op.drop_constraint('fk_quotes_converted_order_id_orders', 'quotes', type_='foreignkey')
    op.drop_index(op.f('ix_quotes_converted_order_id'), table_name='quotes')
    op.drop_column('quotes', 'converted_order_id')
