"""mark a removed vehicle stock line instead of deleting it

Revision ID: d81f4c2b7a95
Revises: c4f1a7d90b23
Create Date: 2026-11-01 22:00:00.000000

An open working day never reaches the database, so the Vehicle Stock × on an
unfinished day is a browser-only cancel.  A day that was really closed is
different: removing a product from it may be necessary, but it must not
destroy the counted evidence.  This adds the three nullable marker columns a
closed-day removal records - when, by whom and why - and keeps the row and its
quantities exactly as they were.  Every existing row is "not removed".
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'd81f4c2b7a95'
down_revision = 'c4f1a7d90b23'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('sales_vehicle_closing_lines', schema=None) as batch_op:
        batch_op.add_column(sa.Column('removed_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('removed_by', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('removal_reason', sa.Text(), nullable=True))
        batch_op.create_foreign_key('fk_sales_closing_removed_by_users', 'users',
                                    ['removed_by'], ['id'])


def downgrade():
    with op.batch_alter_table('sales_vehicle_closing_lines', schema=None) as batch_op:
        batch_op.drop_constraint('fk_sales_closing_removed_by_users', type_='foreignkey')
        batch_op.drop_column('removal_reason')
        batch_op.drop_column('removed_by')
        batch_op.drop_column('removed_at')
