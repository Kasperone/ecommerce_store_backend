"""company EU VAT-ID + VIES confirmation fields

Phase-0 tax U3 (the reverse-charge chain): capture a company's country-prefixed
EU VAT-ID and cache the outcome of an explicit VIES validation on the row.
Distinct from ``companies.tax_id`` (the bare Polish NIP) — a new column, not a
widening. All fields are unconfirmed by default: a company is domestic-taxed
until an explicit admin/portal check succeeds (validation is never on the
checkout path).

Revision ID: b3c4d5e6f7a8
Revises: e7f8a9b0c1d2
Create Date: 2026-07-22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b3c4d5e6f7a8"
down_revision: str | None = "e7f8a9b0c1d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("companies", sa.Column("vat_id", sa.String(length=20), nullable=True))
    op.add_column(
        "companies",
        sa.Column("vies_valid", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column("companies", sa.Column("vies_confirmed_at", sa.DateTime(), nullable=True))
    op.add_column(
        "companies", sa.Column("vies_confirmed_name", sa.String(length=255), nullable=True)
    )
    op.add_column("companies", sa.Column("vies_last_checked_at", sa.DateTime(), nullable=True))
    op.add_column("companies", sa.Column("vies_request_id", sa.String(length=64), nullable=True))


def downgrade() -> None:
    op.drop_column("companies", "vies_request_id")
    op.drop_column("companies", "vies_last_checked_at")
    op.drop_column("companies", "vies_confirmed_name")
    op.drop_column("companies", "vies_confirmed_at")
    op.drop_column("companies", "vies_valid")
    op.drop_column("companies", "vat_id")
