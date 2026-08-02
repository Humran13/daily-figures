"""add daily_review_sessions and daily_review_product_states

Final pre-deployment correction — Manager/Super Administrator Daily
Figures review-and-submit workflow. Two new additive tables, no changes
to any existing table:

  daily_review_sessions — one row per (date, shift) review identity
  (enforced by a unique constraint), tracking status
  (in_progress/submitted/reopened) and who started/submitted/reopened it.
  Metadata only — never stores a stock total, never a source-book
  movement, never a duplicate DailyFigure row.

  daily_review_product_states — one row per (review_session, product)
  once a Manager/Super Administrator has reviewed or skipped that product
  during that session; absence of a row means "not yet reviewed".

See webapp/models/daily_review_session.py and
webapp/services/daily_review_service.py for the full design rationale.

Revision ID: a3f7c92e1d68
Revises: 06658bb730c0
Create Date: 2026-08-02 09:00:00.000000

"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = 'a3f7c92e1d68'
down_revision = '06658bb730c0'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'daily_review_sessions',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('date', sa.String(length=10), nullable=False),
        sa.Column('shift', sa.String(length=10), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False, server_default='in_progress'),
        sa.Column('started_by', sa.Integer(), nullable=True),
        sa.Column('started_role', sa.String(length=20), nullable=True),
        sa.Column('started_at', sa.DateTime(), nullable=False),
        sa.Column('submitted_by', sa.Integer(), nullable=True),
        sa.Column('submitted_role', sa.String(length=20), nullable=True),
        sa.Column('submitted_at', sa.DateTime(), nullable=True),
        sa.Column('reopened_by', sa.Integer(), nullable=True),
        sa.Column('reopened_at', sa.DateTime(), nullable=True),
        sa.Column('reopen_reason', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['started_by'], ['users.id']),
        sa.ForeignKeyConstraint(['submitted_by'], ['users.id']),
        sa.ForeignKeyConstraint(['reopened_by'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('date', 'shift', name='uq_daily_review_session_date_shift'),
    )
    with op.batch_alter_table('daily_review_sessions', schema=None) as batch_op:
        batch_op.create_index('ix_daily_review_sessions_date', ['date'], unique=False)
        batch_op.alter_column('status', server_default=None)

    op.create_table(
        'daily_review_product_states',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('review_session_id', sa.Integer(), nullable=False),
        sa.Column('product_id', sa.Integer(), nullable=False),
        sa.Column('state', sa.String(length=20), nullable=False),
        sa.Column('reason', sa.Text(), nullable=True),
        sa.Column('updated_by', sa.Integer(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['review_session_id'], ['daily_review_sessions.id']),
        sa.ForeignKeyConstraint(['product_id'], ['products.id']),
        sa.ForeignKeyConstraint(['updated_by'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('review_session_id', 'product_id', name='uq_daily_review_product_state'),
    )
    with op.batch_alter_table('daily_review_product_states', schema=None) as batch_op:
        batch_op.create_index('ix_daily_review_product_states_review_session_id', ['review_session_id'], unique=False)
        batch_op.create_index('ix_daily_review_product_states_product_id', ['product_id'], unique=False)


def downgrade():
    with op.batch_alter_table('daily_review_product_states', schema=None) as batch_op:
        batch_op.drop_index('ix_daily_review_product_states_product_id')
        batch_op.drop_index('ix_daily_review_product_states_review_session_id')
    op.drop_table('daily_review_product_states')

    with op.batch_alter_table('daily_review_sessions', schema=None) as batch_op:
        batch_op.drop_index('ix_daily_review_sessions_date')
    op.drop_table('daily_review_sessions')
