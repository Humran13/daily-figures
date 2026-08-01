"""add opening_stock_source provenance to daily_figures

Production hotfix — proven failure: Compact Corporate's 1 August row
showed Opening Stock frozen at 0 even though 30 July had finalized
Returns/Production/Issued producing a Closing Stock of 96.11 Ctns.
Investigation traced this to a THIRD, distinct mechanism from the two
already fixed by e91b6f3a2c07/d44646dd7efe: 1 August was completed
("Already inputted by admin") BEFORE 30 July's historical movement was
ever entered. At that moment, get_prior_closing_base_qty() correctly
found nothing before 1 August at all (no anchor, no finalized movement
yet) — so upsert_daily_figure() correctly treated it as this product's
genuine first-ever period and anchored it (opening_stock_is_override=
True). That was the right call *at the time*. But once 30 July's
Returns/Production/Issued were later finalized, nothing ever re-examined
whether 1 August was still entitled to be trusted as "the first period"
— a bare boolean can't express "this was only ever true because nothing
earlier had been entered yet," so the row stayed frozen forever.

The fix (see webapp/services/stock_service.py's _is_trusted_anchor()) is
architectural, not a one-time repair: every DailyFigure row now carries
an explicit opening_stock_source telling the anchor logic WHY it might be
trusted —
  - derived            — never an anchor.
  - initial_manual     — a genuine first-ever entry (any role) or a
                          legacy-migration row. Anchor-eligible, but
                          re-validated against finalized history on
                          EVERY read (not just once, not just by a
                          migration) — the moment earlier movement is
                          entered, it stops being trusted automatically.
  - manual_correction  — an elevated user's submission that provably
                          differed from live derivation at the moment it
                          was saved. Trusted unconditionally, forever —
                          a deliberate correction is never second-guessed
                          by history added later.
  - legacy_inferred     — inherited from rows written before this column
                          existed, where intent can't be confidently
                          recovered. Treated exactly like initial_manual
                          for trust purposes; kept distinguishable for
                          audit/reporting review.
opening_stock_is_override is kept (nothing outside this model reads it
directly) purely for backward compatibility, mirrored automatically by
every write path — opening_stock_source is now the sole authority.

Classification of EXISTING rows (every opening_stock_is_override=True row
across every product, not limited to any specific product, packaging
family, or "the earliest row" — see d44646dd7efe's narrower scope, which
this migration deliberately broadens per the production hotfix's
explicit instruction), using the best available evidence, per row:

  1. notes matching "Migrated from legacy entries.id=%" (the exact marker
     webapp/services/legacy_migration.py writes) -> initial_manual. Firm,
     positive evidence this is a genuine historical record.
  2. Else, an audit_log "upsert" entry for this exact date|shift|product
     whose recorded before/after Opening Stock actually differ, whose
     recorded "after" matches this row's current stored value, and whose
     actor's CURRENT role is manager or super_admin -> manual_correction.
     Deliberate, evidenced value-changing corrections are preserved.
  3. Else, if finalized Production/Returns/Dispatch/Adjustment activity
     is proven to exist strictly before this row's date+shift ->
     derived. This is the "rows created automatically or marked by the
     flawed migration must become derived" case — we have positive proof
     this row cannot legitimately be "the first period."
  4. Else (no movement before it, no other evidence either way) ->
     legacy_inferred. Included in the completion report for manual
     review; behaviorally identical to initial_manual (always
     live-revalidated), so this classification carries zero risk of
     wrongly overriding earlier finalized movement even if the guess
     turns out wrong later.

Every currently opening_stock_is_override=False row becomes/stays
"derived" (the column's own default). No DailyFigure row is ever
deleted; no stored opening_cartons/packs/pieces/base_qty value is ever
changed by this migration — only opening_stock_source (and, for
consistency, opening_stock_is_override, kept in sync) are reclassified.

Revision ID: 06658bb730c0
Revises: d44646dd7efe
Create Date: 2026-08-01 12:00:00.000000

"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = '06658bb730c0'
down_revision = 'd44646dd7efe'
branch_labels = None
depends_on = None

_SHIFT_ORDER_SQL = "(CASE WHEN shift = 'Day' THEN 0 ELSE 1 END)"

SOURCE_DERIVED = "derived"
SOURCE_INITIAL_MANUAL = "initial_manual"
SOURCE_MANUAL_CORRECTION = "manual_correction"
SOURCE_LEGACY_INFERRED = "legacy_inferred"


def _any_finalized_movement_before(bind, product_id, row_date, row_shift_order):
    """Identical existence check to d44646dd7efe's — kept self-contained
    here (no cross-migration import) matching this project's established
    migration style of never depending on another migration module."""
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

    returns = bind.execute(sa.text("""
        SELECT EXISTS(
            SELECT 1 FROM return_lines rl
            JOIN return_records rr ON rr.id = rl.return_id
            WHERE rr.status = 'finalized' AND rl.product_id = :pid
              AND (rr.date < :row_date OR (rr.date = :row_date AND :row_shift_order > 0))
        )
    """), {"pid": product_id, "row_date": row_date, "row_shift_order": row_shift_order}).scalar()
    return bool(returns)


def _has_evidenced_correction(bind, product_id, product_name, date, shift, current_base_qty):
    return bool(bind.execute(sa.text("""
        SELECT EXISTS(
            SELECT 1 FROM audit_log al
            JOIN users u ON u.id = al.user_id
            WHERE al.entity_type = 'daily_figure'
              AND al.action = 'upsert'
              AND al.entity_id = (:date || '|' || :shift || '|' || :product_name)
              AND u.role IN ('manager', 'super_admin')
              AND CAST(json_extract(al.after_json, '$.opening.base_qty') AS INTEGER) = :current_base_qty
              AND CAST(json_extract(al.before_json, '$.opening.base_qty') AS INTEGER)
                  IS NOT CAST(json_extract(al.after_json, '$.opening.base_qty') AS INTEGER)
        )
    """), {
        "date": date, "shift": shift, "product_name": product_name,
        "current_base_qty": current_base_qty,
    }).scalar())


def _is_legacy_migrated(bind, row_id):
    notes = bind.execute(sa.text(
        "SELECT notes FROM daily_figures WHERE id = :id"
    ), {"id": row_id}).scalar()
    return bool(notes) and notes.startswith("Migrated from legacy entries.id=")


def upgrade():
    with op.batch_alter_table('daily_figures', schema=None) as batch_op:
        batch_op.add_column(sa.Column('opening_stock_source', sa.String(length=20),
                                       nullable=False, server_default=SOURCE_DERIVED))
    with op.batch_alter_table('daily_figures', schema=None) as batch_op:
        batch_op.alter_column('opening_stock_source', server_default=None)

    bind = op.get_bind()
    rows = bind.execute(sa.text(
        "SELECT df.id, df.product_id, df.date, df.shift, df.opening_base_qty, p.name AS product_name "
        "FROM daily_figures df JOIN products p ON p.id = df.product_id "
        "WHERE df.opening_stock_is_override = 1"
    )).fetchall()

    for row_id, product_id, date, shift, base_qty, product_name in rows:
        if _is_legacy_migrated(bind, row_id):
            source = SOURCE_INITIAL_MANUAL
        elif _has_evidenced_correction(bind, product_id, product_name, date, shift, base_qty):
            source = SOURCE_MANUAL_CORRECTION
        else:
            row_shift_order = 0 if shift == "Day" else 1
            if _any_finalized_movement_before(bind, product_id, date, row_shift_order):
                source = SOURCE_DERIVED
            else:
                source = SOURCE_LEGACY_INFERRED

        bind.execute(
            sa.text("UPDATE daily_figures SET opening_stock_source = :source, "
                    "opening_stock_is_override = :is_override WHERE id = :id"),
            {"source": source, "is_override": 0 if source == SOURCE_DERIVED else 1, "id": row_id},
        )


def downgrade():
    with op.batch_alter_table('daily_figures', schema=None) as batch_op:
        batch_op.drop_column('opening_stock_source')
