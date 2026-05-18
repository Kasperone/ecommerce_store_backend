"""reconcile verification_tokens schema drift

Aligns the verification_tokens table with the model in
app/models/verification_token.py (and BaseModel/TimestampMixin):

- token: VARCHAR(36) -> String(255)
- created_at / updated_at: drop server_default and set NOT NULL
  (Python-side default=_utcnow on TimestampMixin handles inserts)
- drop redundant table-level UniqueConstraint 'verification_tokens_token_key'
  (the column-level unique=True, index=True is already covered by the
  ix_verification_tokens_token unique index from the original migration)
- add ix_verification_tokens_id to match BaseModel.id's index=True

After applying, `alembic revision --autogenerate` against this head produces
an empty body.

Revision ID: 9fe7e45d1e55
Revises: c5e1a8d4f2b9
Create Date: 2026-05-18 15:03:22.969266

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '9fe7e45d1e55'
down_revision: Union[str, None] = 'c5e1a8d4f2b9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        'verification_tokens', 'token',
        existing_type=sa.VARCHAR(length=36),
        type_=sa.String(length=255),
        existing_nullable=False,
    )
    op.alter_column(
        'verification_tokens', 'created_at',
        existing_type=postgresql.TIMESTAMP(),
        existing_nullable=True,
        nullable=False,
        existing_server_default=sa.text('now()'),
        server_default=None,
    )
    op.alter_column(
        'verification_tokens', 'updated_at',
        existing_type=postgresql.TIMESTAMP(),
        existing_nullable=True,
        nullable=False,
        existing_server_default=sa.text('now()'),
        server_default=None,
    )
    op.drop_constraint(
        op.f('verification_tokens_token_key'), 'verification_tokens', type_='unique',
    )
    op.create_index(
        op.f('ix_verification_tokens_id'), 'verification_tokens', ['id'], unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f('ix_verification_tokens_id'), table_name='verification_tokens')
    op.create_unique_constraint(
        op.f('verification_tokens_token_key'),
        'verification_tokens',
        ['token'],
        postgresql_nulls_not_distinct=False,
    )
    op.alter_column(
        'verification_tokens', 'updated_at',
        existing_type=postgresql.TIMESTAMP(),
        existing_nullable=False,
        nullable=True,
        server_default=sa.text('now()'),
    )
    op.alter_column(
        'verification_tokens', 'created_at',
        existing_type=postgresql.TIMESTAMP(),
        existing_nullable=False,
        nullable=True,
        server_default=sa.text('now()'),
    )
    op.alter_column(
        'verification_tokens', 'token',
        existing_type=sa.String(length=255),
        type_=sa.VARCHAR(length=36),
        existing_nullable=False,
    )
