"""seed Spare Parts master data from the cleaned workbook dataset

Production fix: the Spare Parts master was empty on a normal deployment
because nothing had ever imported it there — the source workbook
(MV MACHINE SPARES.xlsx) is a local file, never committed and never
required at runtime. This migration inserts the already-cleaned,
committed dataset (webapp/seed_data/spare_parts_master_seed.json — 237
records: name-normalized, BR/BRG collapsed into "Bearing"/"Linear
Bearing", Model+Size combined into Specifications, unambiguous Selling
Price parsed, Buying Price always left NULL) via
webapp/seed_data/spare_part_seed.py's seed_spare_parts_master(), the
exact same function this project's test suite exercises directly
(tests build their schema via db.create_all(), never real Alembic
migrations, so sharing this one function is what keeps the migration and
the tests on one code path).

Insert-only and idempotent: an existing spare_parts row matching a seed
record's identity (normalized name + specifications) is left completely
untouched, so legitimate user edits (including a manually-entered
Selling Price) made between deployments can never be overwritten by a
later rerun. Creates zero SparePartMovement rows, zero opening balances,
zero stock quantities — every seeded spare starts at the usual
ledger-derived current stock of 0, which is correct; real stock is
recorded afterward through the normal Stock In / Adjustment / Opening
Balance workflow. No schema change.

downgrade() is a deliberate no-op: deleting seeded master rows on a
rollback could destroy real work done against them since (further
manual edits, category/price assignment, or stock movements that now
reference their ids).

Revision ID: a7c9d1e3f5b8
Revises: f5a7b9c2d4e6
Create Date: 2026-10-04 00:00:00.000000

"""
from alembic import op

from webapp.seed_data.spare_part_seed import seed_spare_parts_master

# revision identifiers, used by Alembic.
revision = 'a7c9d1e3f5b8'
down_revision = 'f5a7b9c2d4e6'
branch_labels = None
depends_on = None


def upgrade():
    seed_spare_parts_master(op.get_bind())


def downgrade():
    # Deliberate no-op — see module docstring. Seeded master data is
    # never deleted by a schema rollback.
    pass
