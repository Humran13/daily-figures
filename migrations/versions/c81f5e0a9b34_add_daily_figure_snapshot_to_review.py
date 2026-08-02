"""add daily_figure_updated_at snapshot to daily_review_product_states

Final review-workflow safety correction, item 1 — concurrent Opening
Stock/notes changes were not being detected. This adds one additive,
nullable column: the DailyFigure row's own updated_at value AS SEEN AT
THE MOMENT a Manager/Super Administrator marked a product reviewed. On
final submission this is compared against the DailyFigure's CURRENT
updated_at — a mismatch means the record's manual fields (Opening Stock,
notes, provenance) changed after the review, and the product is demoted
back to "not yet reviewed" until it's looked at again. This is completely
independent of the existing source_touched_since check (Dispatch/Returns/
Production book activity) — see webapp/services/daily_review_service.py.

Revision ID: c81f5e0a9b34
Revises: a3f7c92e1d68
Create Date: 2026-08-02 10:00:00.000000

"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = 'c81f5e0a9b34'
down_revision = 'a3f7c92e1d68'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('daily_review_product_states', schema=None) as batch_op:
        batch_op.add_column(sa.Column('daily_figure_updated_at', sa.DateTime(), nullable=True))


def downgrade():
    with op.batch_alter_table('daily_review_product_states', schema=None) as batch_op:
        batch_op.drop_column('daily_figure_updated_at')
