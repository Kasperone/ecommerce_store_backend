"""add quotes.requested_by_user_id + widen status CHECK to include 'requested'

Customer-facing B2B quote-request (Wave C / B2B-1). A customer-requested quote
enters in status ``requested`` and records who requested it
(``requested_by_user_id`` — the company's primary contact at request time). The
status CHECK constraint is dropped and recreated to include the new value.

Revision ID: e3f4a5b6c7d8
Revises: d2e3f4a5b6c7
Create Date: 2026-06-02 09:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e3f4a5b6c7d8'
down_revision: Union[str, None] = 'd2e3f4a5b6c7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Status value sets — kept literal here (a migration is a frozen snapshot of the
# schema at this revision and must not import the evolving app enum).
_OLD_STATUSES = ('draft', 'sent', 'accepted', 'rejected', 'expired')
_NEW_STATUSES = ('draft', 'requested', 'sent', 'accepted', 'rejected', 'expired')


def _check_sql(values: tuple[str, ...]) -> str:
    return "status IN ('" + "', '".join(values) + "')"


def upgrade() -> None:
    op.add_column(
        'quotes',
        sa.Column('requested_by_user_id', sa.Integer(), nullable=True),
    )
    op.create_index(
        op.f('ix_quotes_requested_by_user_id'),
        'quotes',
        ['requested_by_user_id'],
        unique=False,
    )
    op.create_foreign_key(
        'fk_quotes_requested_by_user_id_users',
        'quotes',
        'users',
        ['requested_by_user_id'],
        ['id'],
        ondelete='SET NULL',
    )
    op.drop_constraint('ck_quotes_status_enum', 'quotes', type_='check')
    op.create_check_constraint('ck_quotes_status_enum', 'quotes', _check_sql(_NEW_STATUSES))


def downgrade() -> None:
    op.drop_constraint('ck_quotes_status_enum', 'quotes', type_='check')
    op.create_check_constraint('ck_quotes_status_enum', 'quotes', _check_sql(_OLD_STATUSES))
    op.drop_constraint('fk_quotes_requested_by_user_id_users', 'quotes', type_='foreignkey')
    op.drop_index(op.f('ix_quotes_requested_by_user_id'), table_name='quotes')
    op.drop_column('quotes', 'requested_by_user_id')
