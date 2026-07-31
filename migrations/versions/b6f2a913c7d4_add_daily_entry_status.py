"""add daily_entry_status

Adds the daily_entry_status table (Stage 7): ownership-lock and
completion tracking for one Date + Shift + Product Daily Figures entry,
kept deliberately separate from daily_figures itself (a DailyFigure row is
only created when Opening Stock is written; this table exists regardless,
including for products whose Opening Stock is locked from a prior period
and have nothing else to submit besides a "No Activity Today" review).
Purely additive — no existing table is touched.

Revision ID: b6f2a913c7d4
Revises: f8b3d1a5e920
Create Date: 2026-08-01 09:00:00.000000

"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = 'b6f2a913c7d4'
down_revision = 'f8b3d1a5e920'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'daily_entry_status',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('date', sa.String(length=10), nullable=False),
        sa.Column('shift', sa.String(length=10), nullable=False),
        sa.Column('product_id', sa.Integer(), nullable=False),
        sa.Column('lock_user_id', sa.Integer(), nullable=True),
        sa.Column('lock_acquired_at', sa.DateTime(), nullable=True),
        sa.Column('completed', sa.Boolean(), nullable=False),
        sa.Column('completion_type', sa.String(length=20), nullable=True),
        sa.Column('completed_by', sa.Integer(), nullable=True),
        sa.Column('completed_at', sa.DateTime(), nullable=True),
        sa.Column('reopened_by', sa.Integer(), nullable=True),
        sa.Column('reopened_at', sa.DateTime(), nullable=True),
        sa.Column('reopen_reason', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['product_id'], ['products.id'], ),
        sa.ForeignKeyConstraint(['lock_user_id'], ['users.id'], ),
        sa.ForeignKeyConstraint(['completed_by'], ['users.id'], ),
        sa.ForeignKeyConstraint(['reopened_by'], ['users.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('date', 'shift', 'product_id', name='uq_daily_entry_status_date_shift_product'),
    )
    with op.batch_alter_table('daily_entry_status', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_daily_entry_status_date'), ['date'], unique=False)
        batch_op.create_index(batch_op.f('ix_daily_entry_status_product_id'), ['product_id'], unique=False)


def downgrade():
    with op.batch_alter_table('daily_entry_status', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_daily_entry_status_product_id'))
        batch_op.drop_index(batch_op.f('ix_daily_entry_status_date'))
    op.drop_table('daily_entry_status')
