"""allow decimal quantities on a sales card

Revision ID: c4f1a7d90b23
Revises: a3d9f1c7b2e4
Create Date: 2026-10-31 16:00:00.000000

A sales card prices what the vehicle actually sold, and a salesperson
sometimes knows a part of a carton as a known equivalent amount ("0.5"), so
the two quantity columns on a priced sale row become exact three-decimal
numerics.  Every existing row holds a whole number and keeps its value: the
column type only widens.  Money columns stay whole UGX integers, and Vehicle
Stock keeps counting whole base units.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c4f1a7d90b23'
down_revision = 'a3d9f1c7b2e4'
branch_labels = None
depends_on = None

_QUANTITY = sa.Numeric(precision=12, scale=3)


def upgrade():
    with op.batch_alter_table('sales_invoice_lines', schema=None) as batch_op:
        batch_op.alter_column('quantity', existing_type=sa.Integer(), type_=_QUANTITY,
                              existing_nullable=False)
        batch_op.alter_column('base_unit_qty', existing_type=sa.Integer(), type_=_QUANTITY,
                              existing_nullable=True)
    # Rebuilding the table for the type change cannot carry CHECK constraints
    # across on SQLite (they are not reflected), so re-state them by name.
    with op.batch_alter_table('sales_invoice_lines', schema=None) as batch_op:
        batch_op.create_check_constraint('ck_sales_invoice_line_quantity', 'quantity > 0')
        batch_op.create_check_constraint('ck_sales_invoice_line_price', 'unit_price_ugx >= 0')
        batch_op.create_check_constraint('ck_sales_invoice_line_total', 'line_total_ugx >= 0')


def downgrade():
    """Narrow both quantity columns back to whole numbers.

    A whole-number column cannot represent a fractional price row, so this
    downgrade is deliberately lossy: a quantity below one is raised to one so
    the table's CHECK constraint still holds.  Money columns are untouched, and
    the loss only happens when someone chooses to go back to whole quantities.
    """
    op.execute("UPDATE sales_invoice_lines SET quantity = 1 WHERE quantity < 1")
    op.execute("UPDATE sales_invoice_lines "
               "SET base_unit_qty = 1 WHERE base_unit_qty IS NOT NULL AND base_unit_qty < 1")
    with op.batch_alter_table('sales_invoice_lines', schema=None) as batch_op:
        batch_op.alter_column('quantity', existing_type=_QUANTITY, type_=sa.Integer(),
                              existing_nullable=False)
        batch_op.alter_column('base_unit_qty', existing_type=_QUANTITY, type_=sa.Integer(),
                              existing_nullable=True)
