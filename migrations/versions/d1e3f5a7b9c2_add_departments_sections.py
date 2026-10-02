"""add departments/sections/user_section_access tables

Store Department/Section Access architecture. Purely additive: three new
tables, no existing table or row is altered. Seeds one department
("store") and two sections ("finished_goods", "spare_parts") under it,
then grants every EXISTING user access to "finished_goods" only — every
account in the system today belongs to what is, until now, the only
application there was (Daily Figures/Dispatch/Returns/Production), so
this preserves everyone's current ability to log in. Nobody is seeded
with "spare_parts" access; that is granted explicitly through Admin.

Safe to re-run against a database that already has these tables/rows
(idempotent seed via existence checks) since Flask-Migrate/Alembic
upgrades are expected to run exactly once per environment but this keeps
the data-seeding half of this migration honest regardless.

Revision ID: d1e3f5a7b9c2
Revises: c2d4e6f8a0b1
Create Date: 2026-10-02 00:00:00.000000

"""
from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = 'd1e3f5a7b9c2'
down_revision = 'c2d4e6f8a0b1'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'departments',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('code', sa.String(length=40), nullable=False),
        sa.Column('name', sa.String(length=120), nullable=False),
        sa.Column('active', sa.Boolean(), nullable=False),
        sa.Column('sort_order', sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('code'),
    )
    with op.batch_alter_table('departments', schema=None) as batch_op:
        batch_op.create_index('ix_departments_code', ['code'], unique=False)

    op.create_table(
        'sections',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('department_id', sa.Integer(), nullable=False),
        sa.Column('code', sa.String(length=40), nullable=False),
        sa.Column('name', sa.String(length=120), nullable=False),
        sa.Column('active', sa.Boolean(), nullable=False),
        sa.Column('sort_order', sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(['department_id'], ['departments.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('code'),
    )
    with op.batch_alter_table('sections', schema=None) as batch_op:
        batch_op.create_index('ix_sections_code', ['code'], unique=False)
        batch_op.create_index('ix_sections_department_id', ['department_id'], unique=False)

    op.create_table(
        'user_section_access',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('section_id', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.ForeignKeyConstraint(['section_id'], ['sections.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_id', 'section_id', name='uq_user_section_access_user_section'),
    )
    with op.batch_alter_table('user_section_access', schema=None) as batch_op:
        batch_op.create_index('ix_user_section_access_user_id', ['user_id'], unique=False)
        batch_op.create_index('ix_user_section_access_section_id', ['section_id'], unique=False)

    conn = op.get_bind()
    meta = sa.MetaData()
    departments = sa.Table('departments', meta, autoload_with=conn)
    sections = sa.Table('sections', meta, autoload_with=conn)
    user_section_access = sa.Table('user_section_access', meta, autoload_with=conn)
    users = sa.Table('users', meta, autoload_with=conn)

    dept_row = conn.execute(sa.select(departments.c.id).where(departments.c.code == 'store')).first()
    if dept_row is None:
        dept_id = conn.execute(
            departments.insert().values(code='store', name='Store Department', active=True, sort_order=0)
        ).inserted_primary_key[0]
    else:
        dept_id = dept_row[0]

    section_ids = {}
    for code, name, sort_order in (
        ('finished_goods', 'Finished Goods', 0),
        ('spare_parts', 'Spare Parts', 1),
    ):
        row = conn.execute(sa.select(sections.c.id).where(sections.c.code == code)).first()
        if row is None:
            section_ids[code] = conn.execute(
                sections.insert().values(
                    department_id=dept_id, code=code, name=name, active=True, sort_order=sort_order,
                )
            ).inserted_primary_key[0]
        else:
            section_ids[code] = row[0]

    finished_goods_id = section_ids['finished_goods']
    existing_grants = {
        row[0]
        for row in conn.execute(
            sa.select(user_section_access.c.user_id).where(user_section_access.c.section_id == finished_goods_id)
        )
    }
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    for row in conn.execute(sa.select(users.c.id)):
        if row[0] in existing_grants:
            continue
        conn.execute(
            user_section_access.insert().values(user_id=row[0], section_id=finished_goods_id, created_at=now)
        )


def downgrade():
    with op.batch_alter_table('user_section_access', schema=None) as batch_op:
        batch_op.drop_index('ix_user_section_access_section_id')
        batch_op.drop_index('ix_user_section_access_user_id')
    op.drop_table('user_section_access')

    with op.batch_alter_table('sections', schema=None) as batch_op:
        batch_op.drop_index('ix_sections_department_id')
        batch_op.drop_index('ix_sections_code')
    op.drop_table('sections')

    with op.batch_alter_table('departments', schema=None) as batch_op:
        batch_op.drop_index('ix_departments_code')
    op.drop_table('departments')
