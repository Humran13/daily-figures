"""add editable vehicle stock figures

Revision ID: a3d9f1c7b2e4
Revises: 19f4c7a2b8d1
Create Date: 2026-10-31 12:00:00.000000

Additive only.  Step 1 (Vehicle Stock) becomes the salesperson's own working
sheet: Previous Return, Given Stock and Total Stock can be corrected for the
day being worked on, and a product can be put on the vehicle before its return
is counted.

The three quantity columns stay NULL while the card still follows the system
figure (most recent counted return / finalized dispatch / Previous Return +
Given Stock), so nothing historical is rewritten and a corrected earlier day
keeps carrying forward.  Existing rows backfill to return_counted = True, which
is exactly what they already mean: a counted Return Today.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'a3d9f1c7b2e4'
down_revision = '19f4c7a2b8d1'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('sales_vehicle_closing_lines', schema=None) as batch_op:
        batch_op.add_column(sa.Column('previous_return_base_qty', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('issued_base_qty', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('total_stock_base_qty', sa.Integer(), nullable=True))
        # server_default only backfills the rows that already exist; the ORM's
        # own Python-side default (True) is the sole source of truth afterwards.
        batch_op.add_column(sa.Column('return_counted', sa.Boolean(), nullable=False, server_default='1'))
    with op.batch_alter_table('sales_vehicle_closing_lines', schema=None) as batch_op:
        batch_op.alter_column('return_counted', server_default=None)


def downgrade():
    with op.batch_alter_table('sales_vehicle_closing_lines', schema=None) as batch_op:
        batch_op.drop_column('return_counted')
        batch_op.drop_column('total_stock_base_qty')
        batch_op.drop_column('issued_base_qty')
        batch_op.drop_column('previous_return_base_qty')
