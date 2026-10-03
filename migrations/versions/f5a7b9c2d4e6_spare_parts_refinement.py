"""spare parts refinement: categories, operational departments, specifications, pricing

Targeted Spare Parts UX/master-data refinement. Purely additive: two new
tables (spare_part_categories, spare_part_departments — the latter is an
OPERATIONAL/factory department, unrelated to webapp.models.section's
"Store Department" application-section concept) plus nullable columns on
spare_parts/machines/spare_part_movements. No existing column is dropped;
model/size on spare_parts are untouched and still populated for imported
rows — this migration only ADDS a derived `specifications` column and
backfills it from the existing model/size values, non-destructively.

Revision ID: f5a7b9c2d4e6
Revises: e3f5a7b9c2d4
Create Date: 2026-10-03 00:00:00.000000

"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = 'f5a7b9c2d4e6'
down_revision = 'e3f5a7b9c2d4'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'spare_part_categories',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=120), nullable=False),
        sa.Column('active', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('name'),
    )
    with op.batch_alter_table('spare_part_categories', schema=None) as batch_op:
        batch_op.create_index('ix_spare_part_categories_name', ['name'], unique=False)

    op.create_table(
        'spare_part_departments',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=120), nullable=False),
        sa.Column('active', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('name'),
    )
    with op.batch_alter_table('spare_part_departments', schema=None) as batch_op:
        batch_op.create_index('ix_spare_part_departments_name', ['name'], unique=False)

    with op.batch_alter_table('spare_parts', schema=None) as batch_op:
        batch_op.add_column(sa.Column('specifications', sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column('category_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('buying_price', sa.Numeric(precision=12, scale=2), nullable=True))
        batch_op.add_column(sa.Column('selling_price', sa.Numeric(precision=12, scale=2), nullable=True))
        batch_op.create_foreign_key('fk_spare_parts_category_id', 'spare_part_categories', ['category_id'], ['id'])
        batch_op.create_index('ix_spare_parts_category_id', ['category_id'], unique=False)

    with op.batch_alter_table('machines', schema=None) as batch_op:
        batch_op.add_column(sa.Column('department_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key('fk_machines_department_id', 'spare_part_departments', ['department_id'], ['id'])
        batch_op.create_index('ix_machines_department_id', ['department_id'], unique=False)

    with op.batch_alter_table('spare_part_movements', schema=None) as batch_op:
        batch_op.add_column(sa.Column('department_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('machine_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            'fk_spare_part_movements_department_id', 'spare_part_departments', ['department_id'], ['id'],
        )
        batch_op.create_foreign_key('fk_spare_part_movements_machine_id', 'machines', ['machine_id'], ['id'])
        batch_op.create_index('ix_spare_part_movements_department_id', ['department_id'], unique=False)
        batch_op.create_index('ix_spare_part_movements_machine_id', ['machine_id'], unique=False)

    # Non-destructive backfill: derive `specifications` from the existing
    # model/size text (never dropped, never overwritten with a guess
    # beyond this conservative join). Same rule as
    # spare_part_service.combine_specifications() so import and this
    # migration can never disagree.
    conn = op.get_bind()
    meta = sa.MetaData()
    spare_parts = sa.Table('spare_parts', meta, autoload_with=conn)
    rows = conn.execute(sa.select(spare_parts.c.id, spare_parts.c.model, spare_parts.c.size)).fetchall()
    for row_id, model, size in rows:
        model = (model or '').strip()
        size = (size or '').strip()
        if model and size:
            specs = f"{model} - {size}"
        elif model:
            specs = model
        elif size:
            specs = size
        else:
            specs = None
        if specs:
            conn.execute(spare_parts.update().where(spare_parts.c.id == row_id).values(specifications=specs))


def downgrade():
    with op.batch_alter_table('spare_part_movements', schema=None) as batch_op:
        batch_op.drop_index('ix_spare_part_movements_machine_id')
        batch_op.drop_index('ix_spare_part_movements_department_id')
        batch_op.drop_constraint('fk_spare_part_movements_machine_id', type_='foreignkey')
        batch_op.drop_constraint('fk_spare_part_movements_department_id', type_='foreignkey')
        batch_op.drop_column('machine_id')
        batch_op.drop_column('department_id')

    with op.batch_alter_table('machines', schema=None) as batch_op:
        batch_op.drop_index('ix_machines_department_id')
        batch_op.drop_constraint('fk_machines_department_id', type_='foreignkey')
        batch_op.drop_column('department_id')

    with op.batch_alter_table('spare_parts', schema=None) as batch_op:
        batch_op.drop_index('ix_spare_parts_category_id')
        batch_op.drop_constraint('fk_spare_parts_category_id', type_='foreignkey')
        batch_op.drop_column('selling_price')
        batch_op.drop_column('buying_price')
        batch_op.drop_column('category_id')
        batch_op.drop_column('specifications')

    with op.batch_alter_table('spare_part_departments', schema=None) as batch_op:
        batch_op.drop_index('ix_spare_part_departments_name')
    op.drop_table('spare_part_departments')

    with op.batch_alter_table('spare_part_categories', schema=None) as batch_op:
        batch_op.drop_index('ix_spare_part_categories_name')
    op.drop_table('spare_part_categories')
