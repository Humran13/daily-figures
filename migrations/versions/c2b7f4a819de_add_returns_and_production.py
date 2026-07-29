"""add returns and production books

Adds the Returns Book (return_records/return_lines) and Production Book
(production_records/production_lines) tables, mirroring dispatches/
dispatch_lines' shape, plus two new feature-flag rows ("returns",
"production"), seeded enabled=True so every existing installation keeps
working exactly as before until a Super Administrator explicitly disables
something. Purely additive — no existing table is touched.

Revision ID: c2b7f4a819de
Revises: e8a1576c5404
Create Date: 2026-07-29 10:00:00.000000

"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = 'c2b7f4a819de'
down_revision = 'e8a1576c5404'
branch_labels = None
depends_on = None

NEW_MODULES = ["returns", "production"]


def upgrade():
    op.create_table(
        'return_records',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('date', sa.String(length=10), nullable=False),
        sa.Column('returned_by_customer_id', sa.Integer(), nullable=True),
        sa.Column('returned_by_name_snapshot', sa.String(length=160), nullable=True),
        sa.Column('signed_by_name', sa.String(length=160), nullable=True),
        sa.Column('received_by', sa.Integer(), nullable=True),
        sa.Column('verified_by', sa.Integer(), nullable=True),
        sa.Column('remarks', sa.Text(), nullable=True),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('created_by', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_by', sa.Integer(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.Column('finalized_by', sa.Integer(), nullable=True),
        sa.Column('finalized_at', sa.DateTime(), nullable=True),
        sa.Column('voided_by', sa.Integer(), nullable=True),
        sa.Column('voided_at', sa.DateTime(), nullable=True),
        sa.Column('void_reason', sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(['created_by'], ['users.id'], ),
        sa.ForeignKeyConstraint(['finalized_by'], ['users.id'], ),
        sa.ForeignKeyConstraint(['received_by'], ['users.id'], ),
        sa.ForeignKeyConstraint(['returned_by_customer_id'], ['customers.id'], ),
        sa.ForeignKeyConstraint(['updated_by'], ['users.id'], ),
        sa.ForeignKeyConstraint(['verified_by'], ['users.id'], ),
        sa.ForeignKeyConstraint(['voided_by'], ['users.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('return_records', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_return_records_created_by'), ['created_by'], unique=False)
        batch_op.create_index(batch_op.f('ix_return_records_date'), ['date'], unique=False)
        batch_op.create_index(batch_op.f('ix_return_records_returned_by_customer_id'), ['returned_by_customer_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_return_records_status'), ['status'], unique=False)

    op.create_table(
        'return_lines',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('return_id', sa.Integer(), nullable=False),
        sa.Column('product_id', sa.Integer(), nullable=False),
        sa.Column('cartons', sa.Integer(), nullable=False),
        sa.Column('packs', sa.Integer(), nullable=False),
        sa.Column('pieces', sa.Integer(), nullable=False),
        sa.Column('base_unit_qty', sa.Integer(), nullable=False),
        sa.Column('packaging_rule_id', sa.Integer(), nullable=False),
        sa.Column('line_notes', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['packaging_rule_id'], ['packaging_rules.id'], ),
        sa.ForeignKeyConstraint(['product_id'], ['products.id'], ),
        sa.ForeignKeyConstraint(['return_id'], ['return_records.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('return_lines', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_return_lines_return_id'), ['return_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_return_lines_product_id'), ['product_id'], unique=False)

    op.create_table(
        'production_records',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('date', sa.String(length=10), nullable=False),
        sa.Column('shift', sa.String(length=10), nullable=False),
        sa.Column('remarks', sa.Text(), nullable=True),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('created_by', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_by', sa.Integer(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.Column('finalized_by', sa.Integer(), nullable=True),
        sa.Column('finalized_at', sa.DateTime(), nullable=True),
        sa.Column('voided_by', sa.Integer(), nullable=True),
        sa.Column('voided_at', sa.DateTime(), nullable=True),
        sa.Column('void_reason', sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(['created_by'], ['users.id'], ),
        sa.ForeignKeyConstraint(['finalized_by'], ['users.id'], ),
        sa.ForeignKeyConstraint(['updated_by'], ['users.id'], ),
        sa.ForeignKeyConstraint(['voided_by'], ['users.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('production_records', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_production_records_created_by'), ['created_by'], unique=False)
        batch_op.create_index(batch_op.f('ix_production_records_date'), ['date'], unique=False)
        batch_op.create_index(batch_op.f('ix_production_records_status'), ['status'], unique=False)

    op.create_table(
        'production_lines',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('production_id', sa.Integer(), nullable=False),
        sa.Column('product_id', sa.Integer(), nullable=False),
        sa.Column('cartons', sa.Integer(), nullable=False),
        sa.Column('packs', sa.Integer(), nullable=False),
        sa.Column('pieces', sa.Integer(), nullable=False),
        sa.Column('base_unit_qty', sa.Integer(), nullable=False),
        sa.Column('packaging_rule_id', sa.Integer(), nullable=False),
        sa.Column('line_notes', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['packaging_rule_id'], ['packaging_rules.id'], ),
        sa.ForeignKeyConstraint(['product_id'], ['products.id'], ),
        sa.ForeignKeyConstraint(['production_id'], ['production_records.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('production_lines', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_production_lines_production_id'), ['production_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_production_lines_product_id'), ['product_id'], unique=False)

    bind = op.get_bind()
    flags_table = sa.table(
        'feature_flags',
        sa.column('module_key', sa.String),
        sa.column('enabled', sa.Boolean),
        sa.column('updated_at', sa.DateTime),
    )
    for module_key in NEW_MODULES:
        bind.execute(flags_table.insert().values(module_key=module_key, enabled=True, updated_at=sa.func.now()))


def downgrade():
    bind = op.get_bind()
    flags_table = sa.table('feature_flags', sa.column('module_key', sa.String))
    bind.execute(flags_table.delete().where(flags_table.c.module_key.in_(NEW_MODULES)))

    with op.batch_alter_table('production_lines', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_production_lines_product_id'))
        batch_op.drop_index(batch_op.f('ix_production_lines_production_id'))
    op.drop_table('production_lines')

    with op.batch_alter_table('production_records', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_production_records_status'))
        batch_op.drop_index(batch_op.f('ix_production_records_date'))
        batch_op.drop_index(batch_op.f('ix_production_records_created_by'))
    op.drop_table('production_records')

    with op.batch_alter_table('return_lines', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_return_lines_product_id'))
        batch_op.drop_index(batch_op.f('ix_return_lines_return_id'))
    op.drop_table('return_lines')

    with op.batch_alter_table('return_records', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_return_records_status'))
        batch_op.drop_index(batch_op.f('ix_return_records_returned_by_customer_id'))
        batch_op.drop_index(batch_op.f('ix_return_records_date'))
        batch_op.drop_index(batch_op.f('ix_return_records_created_by'))
    op.drop_table('return_records')
