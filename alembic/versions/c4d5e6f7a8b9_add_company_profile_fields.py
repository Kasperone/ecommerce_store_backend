"""add company profile fields

Wave D / B2B-2 slice 4 — the B2B company-profile fields a COMPANY_ADMIN edits via
``PATCH /companies/me`` (and that a self-signup / admin-directory company starts
without). All **nullable** additive columns on ``companies``:

- ``regon`` — Polish business registry number (alongside ``tax_id`` = NIP/VAT id).
- ``billing_address_{street,city,postal_code,country,state}`` — the invoice
  address (distinct from the per-user shipping address).
- ``default_currency`` — the company's preferred 3-letter ISO currency.

Pure additive, no backfill, no constraints — a clean ``add_column`` / ``drop_column``
pair. Chains off the ``company_users`` migration (slice 3) so this is the single
head after Wave D's BE batch.

Revision ID: c4d5e6f7a8b9
Revises: 2b1ffb455ef9
Create Date: 2026-06-02 11:05:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c4d5e6f7a8b9'
down_revision: Union[str, None] = '2b1ffb455ef9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('companies', sa.Column('regon', sa.String(length=32), nullable=True))
    op.add_column(
        'companies', sa.Column('billing_address_street', sa.String(length=255), nullable=True)
    )
    op.add_column(
        'companies', sa.Column('billing_address_city', sa.String(length=128), nullable=True)
    )
    op.add_column(
        'companies', sa.Column('billing_address_postal_code', sa.String(length=32), nullable=True)
    )
    op.add_column(
        'companies', sa.Column('billing_address_country', sa.String(length=128), nullable=True)
    )
    op.add_column(
        'companies', sa.Column('billing_address_state', sa.String(length=128), nullable=True)
    )
    op.add_column('companies', sa.Column('default_currency', sa.String(length=3), nullable=True))


def downgrade() -> None:
    op.drop_column('companies', 'default_currency')
    op.drop_column('companies', 'billing_address_state')
    op.drop_column('companies', 'billing_address_country')
    op.drop_column('companies', 'billing_address_postal_code')
    op.drop_column('companies', 'billing_address_city')
    op.drop_column('companies', 'billing_address_street')
    op.drop_column('companies', 'regon')
