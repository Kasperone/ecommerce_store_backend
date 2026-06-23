"""Persist the chosen InPost Paczkomat (parcel locker) point on the order.

82% of PL online buyers pick a parcel locker (93% of those InPost). When the
chosen shipping method is an InPost locker, checkout's Geowidget point-picker
yields a specific point; we snapshot its id + human address on the order so the
order summary, admin, and the shipping label all reference the exact locker.
Both NULL for courier-to-address orders.

Revision ID: c9e5f3a7b1d8
Revises: b8d4e1f2a3c5
Create Date: 2026-06-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c9e5f3a7b1d8"
down_revision: str | None = "b8d4e1f2a3c5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("orders", sa.Column("inpost_point_id", sa.String(length=32), nullable=True))
    op.add_column("orders", sa.Column("inpost_point_name", sa.String(length=255), nullable=True))


def downgrade() -> None:
    op.drop_column("orders", "inpost_point_name")
    op.drop_column("orders", "inpost_point_id")
