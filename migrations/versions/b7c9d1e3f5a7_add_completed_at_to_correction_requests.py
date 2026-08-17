"""add completed_at to correction_requests

Full targeted Operator correction/void/requests/notification package —
the approve/consume/expire grant lifecycle needs its own "when was the
one permitted correction actually submitted" timestamp, distinct from
reviewed_at (which now doubles as the grant's START instant). Purely
additive: one new nullable column, no existing data touched. New logical
status values ("completed"/"expired") need no migration at all — `status`
was always a plain unconstrained String(20) column (see
webapp/models/correction_request.py).

Revision ID: b7c9d1e3f5a7
Revises: a1b2c3d4e5f6
Create Date: 2026-08-13 09:00:00.000000

"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = 'b7c9d1e3f5a7'
down_revision = 'a1b2c3d4e5f6'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('correction_requests', schema=None) as batch_op:
        batch_op.add_column(sa.Column('completed_at', sa.DateTime(), nullable=True))


def downgrade():
    with op.batch_alter_table('correction_requests', schema=None) as batch_op:
        batch_op.drop_column('completed_at')
