"""b2b payment-term templates + role gating

Revision ID: 0cebf9c7410a
Revises: 283c32914661
Create Date: 2026-07-16 10:47:36.609206

Generalises Program-2 net terms: the bare ``companies.net_terms_days`` int is
replaced by a named ``PaymentTerm`` template (net_days / due_on_receipt /
fixed_date) that a company points at via ``payment_term_id``. Adds
``net_terms_allowed_roles`` (per-company role gate; defaults to
admin+approver+buyer). Existing net-terms companies are backfilled: each distinct
``net_terms_days`` value becomes a "Net N" template and the companies are
repointed at it before the old column is dropped.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0cebf9c7410a'
down_revision: Union[str, None] = '283c32914661'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_DEFAULT_ROLES = '["COMPANY_ADMIN", "APPROVER", "BUYER"]'


def upgrade() -> None:
    op.create_table(
        'payment_terms',
        sa.Column('name', sa.String(length=100), nullable=False),
        sa.Column('term_type', sa.String(length=20), nullable=False),
        sa.Column('net_days', sa.Integer(), nullable=True),
        sa.Column('due_date', sa.Date(), nullable=True),
        sa.Column('is_active', sa.Boolean(), server_default='true', nullable=False),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "(term_type = 'net_days' AND net_days IS NOT NULL) "
            "OR (term_type = 'fixed_date' AND due_date IS NOT NULL) "
            "OR (term_type = 'due_on_receipt')",
            name='ck_payment_terms_shape',
        ),
        sa.CheckConstraint(
            "term_type IN ('net_days', 'due_on_receipt', 'fixed_date')",
            name='ck_payment_terms_type_enum',
        ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_payment_terms_id'), 'payment_terms', ['id'], unique=False)

    op.add_column('companies', sa.Column('payment_term_id', sa.Integer(), nullable=True))
    # NOT NULL needs a server_default so existing rows populate; the model sets it
    # in Python on new rows, and the default matches DEFAULT_NET_TERMS_ROLES.
    op.add_column(
        'companies',
        sa.Column(
            'net_terms_allowed_roles',
            sa.JSON(),
            server_default=sa.text(f"'{_DEFAULT_ROLES}'"),
            nullable=False,
        ),
    )
    op.create_index(
        op.f('ix_companies_payment_term_id'), 'companies', ['payment_term_id'], unique=False
    )
    op.create_foreign_key(
        'fk_companies_payment_term_id',
        'companies',
        'payment_terms',
        ['payment_term_id'],
        ['id'],
        ondelete='SET NULL',
    )

    # --- backfill: net_terms_days -> a "Net N" payment_term per distinct value ---
    bind = op.get_bind()
    distinct_days = bind.execute(
        sa.text(
            "SELECT DISTINCT net_terms_days FROM companies "
            "WHERE net_terms_days IS NOT NULL"
        )
    ).scalars().all()
    for days in distinct_days:
        term_id = bind.execute(
            sa.text(
                "INSERT INTO payment_terms "
                "(name, term_type, net_days, is_active, created_at, updated_at) "
                "VALUES (:name, 'net_days', :days, true, NOW(), NOW()) RETURNING id"
            ),
            {"name": f"Net {days}", "days": days},
        ).scalar_one()
        bind.execute(
            sa.text(
                "UPDATE companies SET payment_term_id = :tid WHERE net_terms_days = :days"
            ),
            {"tid": term_id, "days": days},
        )

    op.drop_column('companies', 'net_terms_days')


def downgrade() -> None:
    op.add_column(
        'companies',
        sa.Column('net_terms_days', sa.INTEGER(), autoincrement=False, nullable=True),
    )
    # Best-effort restore: net_days terms map back to the int; due_on_receipt /
    # fixed_date terms have no int equivalent and leave net_terms_days NULL.
    op.execute(
        "UPDATE companies c SET net_terms_days = pt.net_days "
        "FROM payment_terms pt "
        "WHERE c.payment_term_id = pt.id AND pt.term_type = 'net_days'"
    )
    op.drop_constraint('fk_companies_payment_term_id', 'companies', type_='foreignkey')
    op.drop_index(op.f('ix_companies_payment_term_id'), table_name='companies')
    op.drop_column('companies', 'net_terms_allowed_roles')
    op.drop_column('companies', 'payment_term_id')
    op.drop_index(op.f('ix_payment_terms_id'), table_name='payment_terms')
    op.drop_table('payment_terms')
