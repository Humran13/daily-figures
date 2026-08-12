"""add correction_requests table

Controlled Operator historical correction/void request workflow — see
webapp/models/correction_request.py. Purely additive: one new table, no
existing table's data is touched, no existing column is altered.

Revision ID: a1b2c3d4e5f6
Revises: f6a30792e68f
Create Date: 2026-08-12 09:00:00.000000

"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = 'a1b2c3d4e5f6'
down_revision = 'f6a30792e68f'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'correction_requests',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('record_type', sa.String(length=20), nullable=False),
        sa.Column('record_id', sa.Integer(), nullable=False),
        sa.Column('action', sa.String(length=20), nullable=False),
        sa.Column('requested_by', sa.Integer(), nullable=False),
        sa.Column('reason', sa.Text(), nullable=False),
        sa.Column('payload_json', sa.Text(), nullable=True),
        sa.Column('before_snapshot_json', sa.Text(), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('reviewed_by', sa.Integer(), nullable=True),
        sa.Column('reviewed_at', sa.DateTime(), nullable=True),
        sa.Column('review_note', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['requested_by'], ['users.id']),
        sa.ForeignKeyConstraint(['reviewed_by'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('correction_requests', schema=None) as batch_op:
        batch_op.create_index('ix_correction_requests_record_type', ['record_type'], unique=False)
        batch_op.create_index('ix_correction_requests_record_id', ['record_id'], unique=False)
        batch_op.create_index('ix_correction_requests_status', ['status'], unique=False)
        batch_op.create_index('ix_correction_requests_created_at', ['created_at'], unique=False)


def downgrade():
    with op.batch_alter_table('correction_requests', schema=None) as batch_op:
        batch_op.drop_index('ix_correction_requests_created_at')
        batch_op.drop_index('ix_correction_requests_status')
        batch_op.drop_index('ix_correction_requests_record_id')
        batch_op.drop_index('ix_correction_requests_record_type')
    op.drop_table('correction_requests')
