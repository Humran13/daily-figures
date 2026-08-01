"""add opening_stock_is_override to daily_figures

Stage 8 correction — fixes a real staging regression: a DailyFigure row
existing exactly at a later date (created by any save that touched that
period, even one that didn't actually change Opening — e.g. an elevated
user paging through with an already-correct value pre-filled) was being
trusted as an authoritative Opening Stock value forever, permanently
freezing that date's carried balance even after earlier Production/
Returns/Dispatch was later corrected. webapp/services/stock_service.py's
daily_figure_view() and get_prior_closing_base_qty() now only ever trust
a row's stored opening_base_qty when this new flag is True; every other
row is treated exactly like "no row exists here" and its Opening Stock is
recomputed live from the nearest genuine anchor on every read.

Backfill rule for existing data (documented, conservative, and safe by
construction — see the completion report for the full reasoning):

  - Each product's chronologically EARLIEST DailyFigure row is marked
    True. This is unambiguous: the application has always required an
    explicit Opening Stock value for a product's first-ever period, so
    this row is, by construction, a genuine deliberate entry.
  - Every OTHER existing row is marked False.

This is the safest rule where historical intent can't be reliably
recovered from data alone (a later row's stored value carries no
persisted signal distinguishing "a deliberate correction" from "a save
that happened to touch this period without changing Opening"). It
directly and completely fixes the reported bug for all existing data: no
existing non-genesis row can continue to silently freeze a later date's
carried balance after this migration. The tradeoff is explicit: if any
later row in existing data really was a deliberate historical correction,
it stops being trusted as an anchor by this migration and that date will
instead show whatever pure carry-forward now derives — Super Administrator
Reset Daily Values already existed as a way to intentionally clear a
row's authority, and re-entering the correction (as a Manager/Super
Administrator) immediately re-establishes it as a real anchor under the
corrected logic. No Opening Stock value is deleted by this migration —
every row's stored cartons/packs/pieces/base_qty are left completely
untouched; only which rows are TRUSTED for live derivation changes.

Revision ID: e91b6f3a2c07
Revises: c7a4e29f1b83
Create Date: 2026-08-16 09:00:00.000000

"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = 'e91b6f3a2c07'
down_revision = 'c7a4e29f1b83'
branch_labels = None
depends_on = None

_SHIFT_ORDER_SQL = "(CASE WHEN shift = 'Day' THEN 0 ELSE 1 END)"


def upgrade():
    with op.batch_alter_table('daily_figures', schema=None) as batch_op:
        batch_op.add_column(sa.Column('opening_stock_is_override', sa.Boolean(), nullable=False, server_default='0'))
    # server_default was only needed to backfill existing rows during the
    # ALTER — drop it afterward so the ORM's own Python-side default
    # (False) is the sole source of truth for new rows, same pattern as
    # every other additive boolean/integer column in this migration
    # history (see d4e7a2c9f631's session_version).
    with op.batch_alter_table('daily_figures', schema=None) as batch_op:
        batch_op.alter_column('opening_stock_is_override', server_default=None)

    bind = op.get_bind()

    # Every row currently defaults to False (via the ALTER's server_default
    # above) — this step only needs to flip the genuine genesis row for
    # each product back to True. Ordering by the same Day-before-Night
    # rule used everywhere else in the app (see stock_service.py's
    # SHIFT_ORDER) via a plain CASE expression, since 'Day' < 'Night'
    # alphabetically would coincidentally also work but shouldn't be
    # relied on implicitly.
    product_ids = [row[0] for row in bind.execute(sa.text(
        "SELECT DISTINCT product_id FROM daily_figures"
    ))]
    for product_id in product_ids:
        earliest_id = bind.execute(sa.text(
            f"SELECT id FROM daily_figures WHERE product_id = :pid "
            f"ORDER BY date ASC, {_SHIFT_ORDER_SQL} ASC, id ASC LIMIT 1"
        ), {"pid": product_id}).scalar()
        if earliest_id is not None:
            bind.execute(
                sa.text("UPDATE daily_figures SET opening_stock_is_override = 1 WHERE id = :id"),
                {"id": earliest_id},
            )


def downgrade():
    with op.batch_alter_table('daily_figures', schema=None) as batch_op:
        batch_op.drop_column('opening_stock_is_override')
