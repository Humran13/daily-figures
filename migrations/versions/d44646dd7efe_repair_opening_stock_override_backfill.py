"""repair opening_stock_is_override rows incorrectly backfilled by e91b6f3a2c07

Stage 8 hotfix. The previous migration (e91b6f3a2c07) backfilled the new
opening_stock_is_override column by treating "each product's
chronologically earliest DailyFigure row" as a safe proxy for "a genuine
initial Opening Stock anchor." That assumption is wrong whenever finalized
Production, Returns, or Dispatch/Adjustment activity for the same product
was recorded at a date+shift strictly BEFORE that earliest row — a
DailyFigure row is not necessarily created at the same time as a
product's first real stock movement (Returns/Production/Dispatch can be
finalized for historical dates well before anyone ever opens Daily
Figures for that product at all), and a later zero/stale row (Reset Daily
Values, No Activity, an elevated no-op save, ad-hoc testing/staging data)
can also legitimately end up being the earliest surviving DailyFigure row.

Proven staging case: product_id=2 ("Compact Standard"), DailyFigure id=2,
date 2026-08-01 Day, opening_base_qty=0 — the previous migration marked
this row an override purely because it was the earliest DailyFigure row
that existed for the product, even though finalized Production of 109.50
Ctns (10,950 base units) already existed on 2026-07-19 Day, strictly
before it. This permanently froze the 1 August page's Opening Stock at 0
instead of deriving 109.50 Ctns from that Production.

Repair rule — deliberately narrower than "every opening_stock_is_override
= true row in the table" (see the completion report for the full
reasoning): only each product's single chronologically-earliest
DailyFigure row is re-examined here — exactly the set the previous
migration's backfill rule could have touched, and nothing else. For that
row, if any finalized Production, Returns, Dispatch, or manual Adjustment
record exists for the same product at any date+shift strictly before it
(using the same Day-before-Night, date-then-shift ordering used
everywhere else — see webapp/services/stock_service.py's SHIFT_ORDER),
opening_stock_is_override is cleared to False. The row then goes back to
being invisible to Opening Stock derivation ("no row exists here"), and
live carry-forward — webapp/services/stock_service.py's
get_prior_closing_base_qty(), hotfixed alongside this migration to derive
from zero using whatever finalized movement predates the first anchor —
takes over on every read.

This is deliberately NOT applied to every opening_stock_is_override=True
row in the table: a genuine Manager/Super Administrator correction
(created through upsert_daily_figure()'s elevated-differs-from-derived
path) is *expected* to coexist with earlier finalized movement — that is
normal and correct; the override exists precisely to deliberately
supersede whatever pure derivation would otherwise say for that period.
Blindly clearing every override that has earlier movement would silently
destroy real, deliberate corrections. Restricting the repair to each
product's single earliest row targets exactly (and only) the set the
flawed backfill rule could have mismarked, without touching anything
created through the app's own (already-correct) write path.

Never modifies opening_cartons/opening_packs/opening_pieces/
opening_base_qty on any row, never deletes a row, and never touches
Production/Returns/Dispatch/Adjustment source records.

Revision ID: d44646dd7efe
Revises: e91b6f3a2c07
Create Date: 2026-08-01 10:00:00.000000

"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = 'd44646dd7efe'
down_revision = 'e91b6f3a2c07'
branch_labels = None
depends_on = None

_SHIFT_ORDER_SQL = "(CASE WHEN shift = 'Day' THEN 0 ELSE 1 END)"


def _any_finalized_movement_before(bind, product_id, row_date, row_shift_order):
    """True if any finalized Production, Returns, Dispatch, or manual
    Adjustment record exists for this product strictly before
    (row_date, row_shift_order) — mirrors
    webapp/services/stock_service.py's runtime existence check, expressed
    as raw SQL (self-contained, no ORM model imports, matching this
    project's established migration style)."""
    production = bind.execute(sa.text(f"""
        SELECT EXISTS(
            SELECT 1 FROM production_lines pl
            JOIN production_records pr ON pr.id = pl.production_id
            WHERE pr.status = 'finalized' AND pl.product_id = :pid
              AND (pr.date < :row_date
                   OR (pr.date = :row_date AND {_SHIFT_ORDER_SQL.replace('shift', 'pr.shift')} < :row_shift_order))
        )
    """), {"pid": product_id, "row_date": row_date, "row_shift_order": row_shift_order}).scalar()
    if production:
        return True

    dispatch = bind.execute(sa.text(f"""
        SELECT EXISTS(
            SELECT 1 FROM dispatch_lines dl
            JOIN dispatches d ON d.id = dl.dispatch_id
            WHERE d.status = 'finalized' AND dl.product_id = :pid
              AND (d.date < :row_date
                   OR (d.date = :row_date AND {_SHIFT_ORDER_SQL.replace('shift', 'd.shift')} < :row_shift_order))
        )
    """), {"pid": product_id, "row_date": row_date, "row_shift_order": row_shift_order}).scalar()
    if dispatch:
        return True

    adjustment = bind.execute(sa.text(f"""
        SELECT EXISTS(
            SELECT 1 FROM stock_adjustments adj
            WHERE adj.product_id = :pid
              AND (adj.date < :row_date
                   OR (adj.date = :row_date AND {_SHIFT_ORDER_SQL.replace('shift', 'adj.shift')} < :row_shift_order))
        )
    """), {"pid": product_id, "row_date": row_date, "row_shift_order": row_shift_order}).scalar()
    if adjustment:
        return True

    # Returns has no shift column — a finalized Return on a date always
    # attributes to that date's Day period (see stock_service.py's
    # return_base_qty()), so it counts as "before" the candidate row when
    # it's on an earlier date, or on the same date with the candidate row
    # itself being that date's Night period.
    returns = bind.execute(sa.text("""
        SELECT EXISTS(
            SELECT 1 FROM return_lines rl
            JOIN return_records rr ON rr.id = rl.return_id
            WHERE rr.status = 'finalized' AND rl.product_id = :pid
              AND (rr.date < :row_date OR (rr.date = :row_date AND :row_shift_order > 0))
        )
    """), {"pid": product_id, "row_date": row_date, "row_shift_order": row_shift_order}).scalar()
    return bool(returns)


def upgrade():
    bind = op.get_bind()

    product_ids = [row[0] for row in bind.execute(sa.text(
        "SELECT DISTINCT product_id FROM daily_figures"
    ))]
    for product_id in product_ids:
        earliest = bind.execute(sa.text(
            f"SELECT id, date, shift FROM daily_figures WHERE product_id = :pid "
            f"ORDER BY date ASC, {_SHIFT_ORDER_SQL} ASC, id ASC LIMIT 1"
        ), {"pid": product_id}).first()
        if earliest is None:
            continue
        row_id, row_date, row_shift = earliest
        currently_override = bind.execute(sa.text(
            "SELECT opening_stock_is_override FROM daily_figures WHERE id = :id"
        ), {"id": row_id}).scalar()
        if not currently_override:
            continue

        row_shift_order = 0 if row_shift == "Day" else 1
        if _any_finalized_movement_before(bind, product_id, row_date, row_shift_order):
            bind.execute(
                sa.text("UPDATE daily_figures SET opening_stock_is_override = 0 WHERE id = :id"),
                {"id": row_id},
            )


def downgrade():
    """Restores e91b6f3a2c07's original (naive) backfill semantics —
    marks each product's chronologically earliest DailyFigure row as an
    override again, unconditionally. This is a well-defined revert to the
    previous migration's own behavior, not a row-by-row undo of exactly
    which rows this migration changed (no such log is kept) — consistent
    with how e91b6f3a2c07 itself is a deterministic function of current
    data, re-running its rule here reproduces its exact original output."""
    bind = op.get_bind()
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
