"""PL B2C invoicing: faktura request (NIP/name on order) + per-product VAT rate.

Two PL-faktura-correctness additions:

- ``orders.invoice_nip`` / ``orders.invoice_name`` — a consumer (or guest) buying
  "na firmę" supplies a NIP + buyer name to print on the faktura.
- ``products.vat_rate`` + ``order_items.vat_rate`` — Polish VAT is not uniform
  (23% standard, 8%, 5%, 0% for books/food/etc.), so each product carries its
  rate and each order line snapshots it at purchase time (like price/name), so a
  faktura reissued after a rate change still reflects what was sold. Both default
  to 23 (standard) — existing rows backfill to standard via ``server_default``.

Revision ID: b8d4e1f2a3c5
Revises: f7b3c1d9e2a4
Create Date: 2026-06-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b8d4e1f2a3c5"
down_revision: str | None = "f7b3c1d9e2a4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("orders", sa.Column("invoice_nip", sa.String(length=16), nullable=True))
    op.add_column("orders", sa.Column("invoice_name", sa.String(length=200), nullable=True))
    op.add_column(
        "products",
        sa.Column("vat_rate", sa.Integer(), nullable=False, server_default="23"),
    )
    op.add_column(
        "order_items",
        sa.Column("vat_rate", sa.Integer(), nullable=False, server_default="23"),
    )


def downgrade() -> None:
    op.drop_column("order_items", "vat_rate")
    op.drop_column("products", "vat_rate")
    op.drop_column("orders", "invoice_name")
    op.drop_column("orders", "invoice_nip")
