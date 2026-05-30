"""partial refunds v2 status and refund_items

Revision ID: 51355143e9f7
Revises: 6791378d96e3
Create Date: 2026-05-30 20:01:31.232713

Adds the ``partially_refunded`` value to the Postgres ``orderstatus`` enum (via
``ALTER TYPE`` — autogenerate doesn't pick this up) and the new ``refund_items``
table that backs the v2 per-line refund picker.

Postgres caveat: ``ALTER TYPE … ADD VALUE`` cannot run inside a transaction
that *uses* the new value, so we run it in an autocommit block; the rest of
the migration (the ``refund_items`` table) runs in the normal transaction.
The new value is made available; no data here references it yet (orders flip
to ``partially_refunded`` at runtime once a partial refund posts via
``POST /api/v1/orders/{id}/refunds``).

Downgrade caveat: Postgres can't cleanly drop an enum value once added. The
downgrade drops the ``refund_items`` table but leaves ``partially_refunded``
on the enum type. In dev/test we never run downgrades; production migrations
are forward-only.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '51355143e9f7'
down_revision: Union[str, None] = '6791378d96e3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ALTER TYPE … ADD VALUE must run outside the migration's transaction on
    # PG <12, and on PG 12+ it cannot be used in the same transaction it was
    # added in. Autocommit block side-steps both: the enum value lands first
    # and is then available to the rest of the migration (and any later one).
    # The PG enum labels are the Python enum **member names** (uppercase) because
    # the existing ``Order.status`` column was created with ``SQLEnum(OrderStatus)``
    # and no ``values_callable``; SQLAlchemy stores ``PENDING``, ``PAID``,
    # ``REFUNDED``, … in the type. The model's string values (``"refunded"``)
    # are still what the Python/HTTP layer sees because str-enum members compare
    # equal to their value, but the wire label here must match the existing
    # ``REFUNDED`` ordering anchor.
    with op.get_context().autocommit_block():
        op.execute(
            "ALTER TYPE orderstatus ADD VALUE IF NOT EXISTS 'PARTIALLY_REFUNDED' "
            "BEFORE 'REFUNDED'"
        )

    op.create_table(
        'refund_items',
        sa.Column('refund_id', sa.Integer(), nullable=False),
        sa.Column('order_item_id', sa.Integer(), nullable=False),
        sa.Column('quantity', sa.Integer(), nullable=False),
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.CheckConstraint('quantity > 0', name='ck_refund_items_quantity_positive'),
        sa.ForeignKeyConstraint(['order_item_id'], ['order_items.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['refund_id'], ['refunds.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('refund_id', 'order_item_id', name='uq_refund_items_refund_order_item'),
    )
    op.create_index(op.f('ix_refund_items_id'), 'refund_items', ['id'], unique=False)
    op.create_index(
        op.f('ix_refund_items_order_item_id'), 'refund_items', ['order_item_id'], unique=False
    )
    op.create_index(op.f('ix_refund_items_refund_id'), 'refund_items', ['refund_id'], unique=False)


def downgrade() -> None:
    # ``partially_refunded`` is intentionally left on the ``orderstatus`` enum
    # — Postgres has no clean ``DROP VALUE`` and our forward-only migration
    # policy means downgrades only matter for failed dev iterations. Any row
    # whose status was ``partially_refunded`` would also need an upstream fix
    # before downgrading.
    op.drop_index(op.f('ix_refund_items_refund_id'), table_name='refund_items')
    op.drop_index(op.f('ix_refund_items_order_item_id'), table_name='refund_items')
    op.drop_index(op.f('ix_refund_items_id'), table_name='refund_items')
    op.drop_table('refund_items')
