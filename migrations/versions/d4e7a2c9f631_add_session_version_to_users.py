"""add session_version to users

Stage 6: lets a super_admin's password reset invalidate the target user's
other already-authenticated sessions (see webapp/auth.py's current_user()
and webapp/routes/admin_users.py's reset_password()). Purely additive —
every existing user row gets session_version=0, which matches what a
session issued before this migration implicitly means (see auth.py's
`session.get("session_version", 0)` default), so no one is logged out by
this migration alone.

Revision ID: d4e7a2c9f631
Revises: c2b7f4a819de
Create Date: 2026-07-31 09:00:00.000000

"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = 'd4e7a2c9f631'
down_revision = 'c2b7f4a819de'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(sa.Column('session_version', sa.Integer(), nullable=False, server_default='0'))
    # server_default was only needed to backfill existing rows during the
    # ALTER — drop it afterward so the ORM's own Python-side default (0) is
    # the single source of truth for new rows, matching every other
    # column's convention in this codebase.
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.alter_column('session_version', server_default=None)


def downgrade():
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_column('session_version')
