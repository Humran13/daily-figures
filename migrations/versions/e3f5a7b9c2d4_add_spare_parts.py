"""add spare parts master + movement ledger tables

Spare Parts inventory (Store Department -> Spare Parts). Purely additive:
eight new tables, no existing table or row touched. No data seeding —
this is greenfield (unlike the departments/sections migration, there is
no pre-existing Spare Parts data to backfill). The Excel workbook import
is a separate, explicit `flask import-spare-parts` CLI step — never run
automatically by this migration.

Revision ID: e3f5a7b9c2d4
Revises: d1e3f5a7b9c2
Create Date: 2026-10-02 00:00:00.000000

"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = 'e3f5a7b9c2d4'
down_revision = 'd1e3f5a7b9c2'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'machines',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('code', sa.String(length=60), nullable=False),
        sa.Column('name', sa.String(length=120), nullable=False),
        sa.Column('active', sa.Boolean(), nullable=False),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('code'),
    )
    with op.batch_alter_table('machines', schema=None) as batch_op:
        batch_op.create_index('ix_machines_code', ['code'], unique=False)

    op.create_table(
        'machine_aliases',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('machine_id', sa.Integer(), nullable=False),
        sa.Column('raw_text', sa.String(length=120), nullable=False),
        sa.Column('normalized_text', sa.String(length=120), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['machine_id'], ['machines.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('normalized_text'),
    )
    with op.batch_alter_table('machine_aliases', schema=None) as batch_op:
        batch_op.create_index('ix_machine_aliases_normalized_text', ['normalized_text'], unique=False)
        batch_op.create_index('ix_machine_aliases_machine_id', ['machine_id'], unique=False)

    op.create_table(
        'spare_suppliers',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=160), nullable=False),
        sa.Column('building', sa.Text(), nullable=True),
        sa.Column('contacts', sa.Text(), nullable=True),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('active', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )

    op.create_table(
        'spare_parts',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('code', sa.String(length=20), nullable=True),
        sa.Column('name', sa.String(length=200), nullable=False),
        sa.Column('model', sa.String(length=120), nullable=True),
        sa.Column('size', sa.String(length=120), nullable=True),
        sa.Column('unit', sa.String(length=20), nullable=False),
        sa.Column('minimum_stock', sa.Numeric(precision=12, scale=3), nullable=True),
        sa.Column('location', sa.String(length=120), nullable=True),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('active', sa.Boolean(), nullable=False),
        sa.Column('current_stock_cache', sa.Numeric(precision=12, scale=3), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('code'),
    )
    with op.batch_alter_table('spare_parts', schema=None) as batch_op:
        batch_op.create_index('ix_spare_parts_name', ['name'], unique=False)
        batch_op.create_index('ix_spare_parts_active', ['active'], unique=False)
        batch_op.create_index('ix_spare_parts_code', ['code'], unique=False)

    op.create_table(
        'spare_part_machines',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('spare_part_id', sa.Integer(), nullable=False),
        sa.Column('machine_id', sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(['spare_part_id'], ['spare_parts.id']),
        sa.ForeignKeyConstraint(['machine_id'], ['machines.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('spare_part_id', 'machine_id', name='uq_spare_part_machine'),
    )
    with op.batch_alter_table('spare_part_machines', schema=None) as batch_op:
        batch_op.create_index('ix_spare_part_machines_spare_part_id', ['spare_part_id'], unique=False)
        batch_op.create_index('ix_spare_part_machines_machine_id', ['machine_id'], unique=False)

    op.create_table(
        'spare_part_suppliers',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('spare_part_id', sa.Integer(), nullable=False),
        sa.Column('supplier_id', sa.Integer(), nullable=False),
        sa.Column('raw_price_text', sa.String(length=120), nullable=True),
        sa.Column('normalized_price', sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['spare_part_id'], ['spare_parts.id']),
        sa.ForeignKeyConstraint(['supplier_id'], ['spare_suppliers.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('spare_part_id', 'supplier_id', 'raw_price_text', name='uq_spare_part_supplier_quote'),
    )
    with op.batch_alter_table('spare_part_suppliers', schema=None) as batch_op:
        batch_op.create_index('ix_spare_part_suppliers_spare_part_id', ['spare_part_id'], unique=False)
        batch_op.create_index('ix_spare_part_suppliers_supplier_id', ['supplier_id'], unique=False)

    op.create_table(
        'spare_part_movements',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('business_date', sa.String(length=10), nullable=False),
        sa.Column('spare_part_id', sa.Integer(), nullable=False),
        sa.Column('movement_type', sa.String(length=20), nullable=False),
        sa.Column('quantity', sa.Numeric(precision=12, scale=3), nullable=False),
        sa.Column('unit_snapshot', sa.String(length=20), nullable=True),
        sa.Column('supplier_id', sa.Integer(), nullable=True),
        sa.Column('reference_doc', sa.String(length=120), nullable=True),
        sa.Column('requested_by', sa.String(length=120), nullable=True),
        sa.Column('approved_by', sa.String(length=120), nullable=True),
        sa.Column('reason', sa.String(length=60), nullable=True),
        sa.Column('remarks', sa.Text(), nullable=True),
        sa.Column('status', sa.String(length=10), nullable=False),
        sa.Column('voided_by', sa.Integer(), nullable=True),
        sa.Column('voided_at', sa.DateTime(), nullable=True),
        sa.Column('void_reason', sa.Text(), nullable=True),
        sa.Column('entered_by_user_id', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['spare_part_id'], ['spare_parts.id']),
        sa.ForeignKeyConstraint(['supplier_id'], ['spare_suppliers.id']),
        sa.ForeignKeyConstraint(['voided_by'], ['users.id']),
        sa.ForeignKeyConstraint(['entered_by_user_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('spare_part_movements', schema=None) as batch_op:
        batch_op.create_index('ix_spare_part_movements_business_date', ['business_date'], unique=False)
        batch_op.create_index('ix_spare_part_movements_spare_part_id', ['spare_part_id'], unique=False)
        batch_op.create_index('ix_spare_part_movements_movement_type', ['movement_type'], unique=False)
        batch_op.create_index('ix_spare_part_movements_status', ['status'], unique=False)

    op.create_table(
        'spare_part_import_rows',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('source_file', sa.String(length=255), nullable=False),
        sa.Column('sheet_name', sa.String(length=80), nullable=False),
        sa.Column('source_row', sa.Integer(), nullable=False),
        sa.Column('raw_json', sa.Text(), nullable=False),
        sa.Column('spare_part_id', sa.Integer(), nullable=True),
        sa.Column('import_batch_id', sa.String(length=40), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['spare_part_id'], ['spare_parts.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('source_file', 'sheet_name', 'source_row', name='uq_spare_part_import_row_source'),
    )


def downgrade():
    op.drop_table('spare_part_import_rows')

    with op.batch_alter_table('spare_part_movements', schema=None) as batch_op:
        batch_op.drop_index('ix_spare_part_movements_status')
        batch_op.drop_index('ix_spare_part_movements_movement_type')
        batch_op.drop_index('ix_spare_part_movements_spare_part_id')
        batch_op.drop_index('ix_spare_part_movements_business_date')
    op.drop_table('spare_part_movements')

    with op.batch_alter_table('spare_part_suppliers', schema=None) as batch_op:
        batch_op.drop_index('ix_spare_part_suppliers_supplier_id')
        batch_op.drop_index('ix_spare_part_suppliers_spare_part_id')
    op.drop_table('spare_part_suppliers')

    with op.batch_alter_table('spare_part_machines', schema=None) as batch_op:
        batch_op.drop_index('ix_spare_part_machines_machine_id')
        batch_op.drop_index('ix_spare_part_machines_spare_part_id')
    op.drop_table('spare_part_machines')

    with op.batch_alter_table('spare_parts', schema=None) as batch_op:
        batch_op.drop_index('ix_spare_parts_code')
        batch_op.drop_index('ix_spare_parts_active')
        batch_op.drop_index('ix_spare_parts_name')
    op.drop_table('spare_parts')

    op.drop_table('spare_suppliers')

    with op.batch_alter_table('machine_aliases', schema=None) as batch_op:
        batch_op.drop_index('ix_machine_aliases_machine_id')
        batch_op.drop_index('ix_machine_aliases_normalized_text')
    op.drop_table('machine_aliases')

    with op.batch_alter_table('machines', schema=None) as batch_op:
        batch_op.drop_index('ix_machines_code')
    op.drop_table('machines')
