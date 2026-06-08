"""campaign segment audience

Revision ID: 85d9d5df29b7
Revises: eeb9da42241f
Create Date: 2026-06-08 11:46:00.982495

Adds the ``segment`` campaign audience and links a campaign to a segment:

* ``campaigns.segment_id`` — nullable FK → ``segments.id`` (ON DELETE SET NULL),
  so deleting a referenced segment nulls the link rather than blocking the delete.
* Extends the ``ck_campaigns_audience_enum`` CHECK to allow ``'segment'`` —
  ``audience`` is a plain ``String(16)`` column guarded by an ``IN (...)`` CHECK
  (NOT a PG enum, so this is a DROP/ADD of the CHECK, not ``ALTER TYPE ADD VALUE``;
  autogenerate does not handle either — hand-added here).
* Adds ``ck_campaigns_segment_audience`` — ``segment_id IS NULL OR
  audience='segment'`` (autogenerate misses CHECKs — hand-added). Deliberately
  ONE-directional: a non-segment campaign may never carry a segment_id, but a
  segment campaign is allowed to have a NULL segment_id *at the DB level* so that
  ``ON DELETE SET NULL`` can null the FK on segment delete without tripping the
  CHECK. The "segment campaign requires a segment_id" half of the invariant is
  enforced at write time in the schema/route layer instead.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '85d9d5df29b7'
down_revision: Union[str, None] = 'eeb9da42241f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. FK column linking a campaign to a segment (nullable; SET NULL on delete).
    op.add_column('campaigns', sa.Column('segment_id', sa.Integer(), nullable=True))
    op.create_foreign_key(
        'fk_campaigns_segment_id_segments',
        'campaigns',
        'segments',
        ['segment_id'],
        ['id'],
        ondelete='SET NULL',
    )

    # 2. Extend the audience CHECK to include the new 'segment' value. The column
    #    is a String(16) with an IN (...) CHECK — not a PG enum — so this is a
    #    DROP + re-ADD of the CHECK (autogenerate does not emit CHECK changes).
    op.drop_constraint('ck_campaigns_audience_enum', 'campaigns', type_='check')
    op.create_check_constraint(
        'ck_campaigns_audience_enum',
        'campaigns',
        "audience IN ('all', 'customers', 'b2b', 'segment')",
    )

    # 3. One-directional invariant: a segment_id is only allowed on a segment
    #    campaign (hand-added — autogenerate misses CHECKs). NOT the full
    #    biconditional, so ON DELETE SET NULL can null the FK on segment delete
    #    without violating the CHECK on the resulting orphan row.
    op.create_check_constraint(
        'ck_campaigns_segment_audience',
        'campaigns',
        "segment_id IS NULL OR audience = 'segment'",
    )


def downgrade() -> None:
    op.drop_constraint('ck_campaigns_segment_audience', 'campaigns', type_='check')

    op.drop_constraint('ck_campaigns_audience_enum', 'campaigns', type_='check')
    op.create_check_constraint(
        'ck_campaigns_audience_enum',
        'campaigns',
        "audience IN ('all', 'customers', 'b2b')",
    )

    op.drop_constraint('fk_campaigns_segment_id_segments', 'campaigns', type_='foreignkey')
    op.drop_column('campaigns', 'segment_id')
