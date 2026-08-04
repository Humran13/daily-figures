"""add ledger cutover tables and daily_figures.cutover_id

Final stock architecture — clean ledger cutover. Adds two new tables
(ledger_cutovers, ledger_cutover_balances) and one additive, nullable
foreign key column (daily_figures.cutover_id) tracing a cutover-created
DailyFigure row back to the LedgerCutover that wrote it. Purely additive:
no existing table's data is touched, no existing column is altered or
dropped. See webapp/models/ledger_cutover.py and
webapp/services/ledger_cutover_service.py.

Revision ID: f6a30792e68f
Revises: c81f5e0a9b34
Create Date: 2026-08-05 09:00:00.000000

"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = 'f6a30792e68f'
down_revision = 'c81f5e0a9b34'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'ledger_cutovers',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('effective_date', sa.String(length=10), nullable=False),
        sa.Column('effective_shift', sa.String(length=10), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('reason', sa.Text(), nullable=True),
        sa.Column('created_by', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('verified_by', sa.Integer(), nullable=True),
        sa.Column('verified_at', sa.DateTime(), nullable=True),
        sa.Column('activated_by', sa.Integer(), nullable=True),
        sa.Column('activated_at', sa.DateTime(), nullable=True),
        sa.Column('cancelled_by', sa.Integer(), nullable=True),
        sa.Column('cancelled_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['created_by'], ['users.id']),
        sa.ForeignKeyConstraint(['verified_by'], ['users.id']),
        sa.ForeignKeyConstraint(['activated_by'], ['users.id']),
        sa.ForeignKeyConstraint(['cancelled_by'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('ledger_cutovers', schema=None) as batch_op:
        batch_op.create_index('ix_ledger_cutovers_effective_date', ['effective_date'], unique=False)

    op.create_table(
        'ledger_cutover_balances',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('cutover_id', sa.Integer(), nullable=False),
        sa.Column('product_id', sa.Integer(), nullable=False),
        sa.Column('cartons', sa.Integer(), nullable=False),
        sa.Column('packs', sa.Integer(), nullable=False),
        sa.Column('pieces', sa.Integer(), nullable=False),
        sa.Column('base_qty', sa.Integer(), nullable=False),
        sa.Column('created_by', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_by', sa.Integer(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['cutover_id'], ['ledger_cutovers.id']),
        sa.ForeignKeyConstraint(['product_id'], ['products.id']),
        sa.ForeignKeyConstraint(['created_by'], ['users.id']),
        sa.ForeignKeyConstraint(['updated_by'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('cutover_id', 'product_id', name='uq_ledger_cutover_balance_product'),
    )
    with op.batch_alter_table('ledger_cutover_balances', schema=None) as batch_op:
        batch_op.create_index('ix_ledger_cutover_balances_cutover_id', ['cutover_id'], unique=False)
        batch_op.create_index('ix_ledger_cutover_balances_product_id', ['product_id'], unique=False)

    with op.batch_alter_table('daily_figures', schema=None) as batch_op:
        batch_op.add_column(sa.Column('cutover_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key('fk_daily_figures_cutover_id', 'ledger_cutovers', ['cutover_id'], ['id'])


def downgrade():
    with op.batch_alter_table('daily_figures', schema=None) as batch_op:
        batch_op.drop_constraint('fk_daily_figures_cutover_id', type_='foreignkey')
        batch_op.drop_column('cutover_id')

    with op.batch_alter_table('ledger_cutover_balances', schema=None) as batch_op:
        batch_op.drop_index('ix_ledger_cutover_balances_product_id')
        batch_op.drop_index('ix_ledger_cutover_balances_cutover_id')
    op.drop_table('ledger_cutover_balances')

    with op.batch_alter_table('ledger_cutovers', schema=None) as batch_op:
        batch_op.drop_index('ix_ledger_cutovers_effective_date')
    op.drop_table('ledger_cutovers')
