"""order reverse-charge treatment + buyer VAT-ID snapshot

Phase-0 tax U4: an order records the tax treatment it was placed under.
``tax_treatment`` defaults to ``domestic`` (every existing order, and every
non-reverse-charge order). ``buyer_vat_id`` / ``buyer_vies_confirmed_at`` snapshot
the confirmation a reverse-charge decision rested on, as audit evidence that can't
be re-derived from the mutable company row.

Revision ID: c5d6e7f8a9b0
Revises: b3c4d5e6f7a8
Create Date: 2026-07-22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c5d6e7f8a9b0"
down_revision: str | None = "b3c4d5e6f7a8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "orders",
        sa.Column(
            "tax_treatment",
            sa.String(length=16),
            nullable=False,
            server_default="domestic",
        ),
    )
    op.add_column("orders", sa.Column("buyer_vat_id", sa.String(length=20), nullable=True))
    op.add_column("orders", sa.Column("buyer_vies_confirmed_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("orders", "buyer_vies_confirmed_at")
    op.drop_column("orders", "buyer_vat_id")
    op.drop_column("orders", "tax_treatment")
