"""guest checkout fields on orders

Guest checkout (Feature A, PR A-be):

1. ``orders.user_id`` becomes nullable — a guest order has no user row. The
   decision is deliberately NOT guest-user rows: those would collide with the
   email-verification-required login flow, break "register later with the same
   email" (unique email), and silently expose guest orders to whoever later
   registers that email. Owner-scoped queries need zero changes — a NULL
   ``user_id`` matches no customer filter and stays visible to admins.
2. ``guest_email`` (indexed) — the order's contact address; required iff the
   order has no user (see the CHECK below). Indexed to back the future
   "claim guest orders at registration" backfill.
3. ``guest_token`` (unique index) — opaque 256-bit tracking token
   (``secrets.token_urlsafe(32)``, ~43 chars). Generated ONLY for guest
   orders; authed orders keep NULL. Backs ``GET /orders/track/{token}``.
4. ``guest_locale`` — email language for the guest (no User.locale to read).
5. CHECK ``ck_orders_user_or_guest_email`` — every order has an owner OR a
   guest contact email; never neither.

No data backfill — every existing order has a non-null ``user_id``, so the
CHECK is satisfied and the new columns stay NULL. Pure DDL, safe on a live DB.

Revision ID: 71b7f8d072af
Revises: 3b7fbb1689a9
Create Date: 2026-06-08 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '71b7f8d072af'
down_revision: Union[str, None] = '3b7fbb1689a9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column('orders', 'user_id', existing_type=sa.INTEGER(), nullable=True)
    op.add_column('orders', sa.Column('guest_email', sa.String(length=255), nullable=True))
    op.add_column('orders', sa.Column('guest_token', sa.String(length=64), nullable=True))
    op.add_column('orders', sa.Column('guest_locale', sa.String(length=5), nullable=True))
    op.create_index(op.f('ix_orders_guest_email'), 'orders', ['guest_email'], unique=False)
    op.create_index(op.f('ix_orders_guest_token'), 'orders', ['guest_token'], unique=True)
    op.create_check_constraint(
        'ck_orders_user_or_guest_email',
        'orders',
        'user_id IS NOT NULL OR guest_email IS NOT NULL',
    )


def downgrade() -> None:
    # NOTE: fails if guest orders (user_id IS NULL) exist — delete or reassign
    # them first; we never silently drop order rows in a migration.
    op.drop_constraint('ck_orders_user_or_guest_email', 'orders', type_='check')
    op.drop_index(op.f('ix_orders_guest_token'), table_name='orders')
    op.drop_index(op.f('ix_orders_guest_email'), table_name='orders')
    op.drop_column('orders', 'guest_locale')
    op.drop_column('orders', 'guest_token')
    op.drop_column('orders', 'guest_email')
    op.alter_column('orders', 'user_id', existing_type=sa.INTEGER(), nullable=False)
