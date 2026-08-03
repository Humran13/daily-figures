from datetime import datetime, timezone

from webapp.extensions import db


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


# Stage 8 production hotfix — a bare boolean ("is this row an override")
# couldn't say WHY a row was trusted, which is exactly what let a genuine
# out-of-order data-entry case (a later period completed before its
# earlier history was ever entered) get permanently stuck: the row
# legitimately became "the first period" at the moment it was saved, but
# a plain True/False can't later tell "that was a deliberate, still-valid
# correction" apart from "that was only ever true because nothing earlier
# had been entered yet." See stock_service.py's _find_anchor_figure() for
# how each source is (or isn't) trusted.
OPENING_STOCK_SOURCE_DERIVED = "derived"
OPENING_STOCK_SOURCE_INITIAL_MANUAL = "initial_manual"
OPENING_STOCK_SOURCE_MANUAL_CORRECTION = "manual_correction"
OPENING_STOCK_SOURCE_LEGACY_INFERRED = "legacy_inferred"
# Legacy-opening-migration investigation — a row created by
# webapp/services/legacy_migration.py's run_legacy_migration() from a
# genuine, independently-reconciled row of the old `entries` spreadsheet
# ledger (Opening + Production + Returns - Issued = Closing, all already
# balanced by the business before migration). Deliberately DISTINCT from
# initial_manual/legacy_inferred: those are *live-revalidated* on every
# read (see OPENING_STOCK_SOURCES_UNCONDITIONAL_ANCHOR below) — which
# turned out to silently demote and discard a legacy row's own
# authoritative historical Opening the moment ANY earlier finalized
# activity existed, including another legacy row's OWN migrated Issued
# StockAdjustment. A legacy ledger row does not need "live revalidation"
# the way an ordinary incidental first-touch save does: the business
# already reconciled it, so it is trusted unconditionally, exactly like a
# manual_correction — never typed by a current Manager, but just as
# authoritative, and still fully auditable/distinguishable via this its
# own source value. No schema migration was required to add this value —
# opening_stock_source is a plain String(20) column with no CHECK
# constraint (see migrations/versions/06658bb730c0).
OPENING_STOCK_SOURCE_LEGACY_MIGRATED_OPENING = "legacy_migrated_opening"
OPENING_STOCK_SOURCES = [
    OPENING_STOCK_SOURCE_DERIVED,
    OPENING_STOCK_SOURCE_INITIAL_MANUAL,
    OPENING_STOCK_SOURCE_MANUAL_CORRECTION,
    OPENING_STOCK_SOURCE_LEGACY_INFERRED,
    OPENING_STOCK_SOURCE_LEGACY_MIGRATED_OPENING,
]
# Sources a row must have to even be a *candidate* anchor at all — every
# other source is invisible to anchor lookup, exactly like "no row exists
# here" (see stock_service._find_anchor_figure()).
OPENING_STOCK_SOURCES_ANCHOR_ELIGIBLE = (
    OPENING_STOCK_SOURCE_INITIAL_MANUAL,
    OPENING_STOCK_SOURCE_MANUAL_CORRECTION,
    OPENING_STOCK_SOURCE_LEGACY_INFERRED,
    OPENING_STOCK_SOURCE_LEGACY_MIGRATED_OPENING,
)
# Only a deliberate, evidenced correction (the submitted value differed
# from live derivation at the moment an elevated user saved it — see
# upsert_daily_figure()) — or a legacy ledger row already reconciled by
# the business before migration — is trusted UNCONDITIONALLY, regardless
# of whether finalized movement is later discovered before it. Every
# other anchor-eligible source is *live-revalidated* on every read: if
# finalized movement now exists before it, it is no longer trusted (see
# _find_anchor_figure()) — this is what lets a later period entered
# before its own history recalculate automatically once that history is
# entered, without needing a fresh migration or a manual reset every time.
OPENING_STOCK_SOURCES_UNCONDITIONAL_ANCHOR = (
    OPENING_STOCK_SOURCE_MANUAL_CORRECTION,
    OPENING_STOCK_SOURCE_LEGACY_MIGRATED_OPENING,
)


class DailyFigure(db.Model):
    """
    The new source of truth for Opening/Return/Production (Issued and
    Closing are never stored here — both are derived at read time: Issued
    from finalized dispatch lines + adjustments, Closing from the formula.
    Storing them would just be a second place for them to go stale.

    The legacy `entries` table (webapp/legacy_entries.py) is left exactly as
    it was — untouched, still queryable — but is no longer the source of
    truth for anything written after this table existed.
    """
    __tablename__ = "daily_figures"

    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.String(10), nullable=False, index=True)
    shift = db.Column(db.String(10), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"), nullable=False, index=True)

    opening_cartons = db.Column(db.Integer, nullable=False, default=0)
    opening_packs = db.Column(db.Integer, nullable=False, default=0)
    opening_pieces = db.Column(db.Integer, nullable=False, default=0)
    opening_base_qty = db.Column(db.Integer, nullable=False, default=0)
    # Stage 8 correction: True only when this row's Opening Stock is a
    # deliberate, authoritative value. Kept for backward compatibility
    # (nothing outside this model reads it directly), but it is no longer
    # the authoritative signal — opening_stock_source below is. Always
    # kept in sync: True iff opening_stock_source is initial_manual,
    # manual_correction, or legacy_inferred (i.e. "anchor-eligible" — see
    # OPENING_STOCK_SOURCES_ANCHOR_ELIGIBLE above).
    opening_stock_is_override = db.Column(db.Boolean, nullable=False, default=False)
    # Stage 8 production hotfix — WHY this row's stored opening is (or
    # isn't) an anchor candidate, one of OPENING_STOCK_SOURCES above. This
    # is the field _find_anchor_figure() actually decides on:
    #   derived            — never an anchor; always recomputed live.
    #   initial_manual     — a product's genuine first-ever entry (any
    #                        role) or a legacy-migration row. Anchor-
    #                        eligible, but only trusted for as long as no
    #                        finalized movement is found before it —
    #                        re-checked on every read, never cached.
    #   manual_correction  — an elevated (Manager/Super Admin) user
    #                        submitted a value that genuinely differed
    #                        from live derivation at that moment. Trusted
    #                        unconditionally, forever, regardless of any
    #                        finalized movement discovered before it —
    #                        that's the entire point of a correction.
    #   legacy_inferred    — inherited from data written before this
    #                        column existed, where historical intent
    #                        can't be reliably recovered (see the
    #                        corrective migration). Treated exactly like
    #                        initial_manual for trust purposes (live-
    #                        revalidated), but kept distinguishable for
    #                        audit/reporting.
    opening_stock_source = db.Column(db.String(20), nullable=False, default=OPENING_STOCK_SOURCE_DERIVED)

    return_cartons = db.Column(db.Integer, nullable=False, default=0)
    return_packs = db.Column(db.Integer, nullable=False, default=0)
    return_pieces = db.Column(db.Integer, nullable=False, default=0)
    return_base_qty = db.Column(db.Integer, nullable=False, default=0)

    production_cartons = db.Column(db.Integer, nullable=False, default=0)
    production_packs = db.Column(db.Integer, nullable=False, default=0)
    production_pieces = db.Column(db.Integer, nullable=False, default=0)
    production_base_qty = db.Column(db.Integer, nullable=False, default=0)

    # Snapshot of the rule used to convert this row's cartons/packs/pieces —
    # same "never reinterpret history" guarantee as dispatch lines.
    packaging_rule_id = db.Column(db.Integer, db.ForeignKey("packaging_rules.id"), nullable=False)

    notes = db.Column(db.Text, nullable=True)
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    created_at = db.Column(db.DateTime(), nullable=False, default=_utcnow)
    updated_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    updated_at = db.Column(db.DateTime(), nullable=False, default=_utcnow, onupdate=_utcnow)

    product = db.relationship("Product")

    __table_args__ = (
        db.UniqueConstraint("date", "shift", "product_id", name="uq_daily_figure_date_shift_product"),
    )


class StockAdjustment(db.Model):
    """
    A manual correction to the Issued total for one date/shift/product —
    the "clearly controlled adjustment process" the spec requires, for
    cases where the real movement of stock isn't (or can't yet be)
    reflected by a dispatch record. Always additive to the audit log,
    never a silent edit of a derived number.
    """
    __tablename__ = "stock_adjustments"

    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.String(10), nullable=False, index=True)
    shift = db.Column(db.String(10), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"), nullable=False, index=True)
    delta_base_qty = db.Column(db.Integer, nullable=False)  # signed: +adds to issued, -reduces it
    reason = db.Column(db.Text, nullable=False)
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    created_at = db.Column(db.DateTime(), nullable=False, default=_utcnow)

    product = db.relationship("Product")

    def to_dict(self):
        return {
            "id": self.id,
            "date": self.date,
            "shift": self.shift,
            "product_id": self.product_id,
            "delta_base_qty": self.delta_base_qty,
            "reason": self.reason,
            "created_by": self.created_by,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class LegacyMigrationFlag(db.Model):
    """
    One row per legacy `entries` row the automatic decoder could not
    confidently convert to exact cartons/packs/pieces — e.g. because the
    product's packs-per-carton or pieces-per-pack ratio exceeds what the
    old single-decimal-digit-per-unit notation could represent. Never
    guessed past; always left for a human to resolve via the admin review
    queue. The source `entries` row is untouched either way.
    """
    __tablename__ = "legacy_migration_flags"

    id = db.Column(db.Integer, primary_key=True)
    entries_row_id = db.Column(db.Integer, nullable=False, index=True)
    date = db.Column(db.String(10), nullable=False)
    shift = db.Column(db.String(10), nullable=False)
    product_name = db.Column(db.String(120), nullable=False)
    field = db.Column(db.String(20), nullable=False)  # 'opening' | 'return_val' | 'production'
    raw_value = db.Column(db.Float, nullable=False)
    reason = db.Column(db.Text, nullable=False)
    resolved = db.Column(db.Boolean, nullable=False, default=False)
    resolved_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    resolved_at = db.Column(db.DateTime(), nullable=True)
    created_at = db.Column(db.DateTime(), nullable=False, default=_utcnow)

    def to_dict(self):
        return {
            "id": self.id,
            "entries_row_id": self.entries_row_id,
            "date": self.date,
            "shift": self.shift,
            "product_name": self.product_name,
            "field": self.field,
            "raw_value": self.raw_value,
            "reason": self.reason,
            "resolved": self.resolved,
        }
