from datetime import datetime, timezone

from webapp.extensions import db


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


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
    # deliberate, authoritative value — a product's genuine first-ever
    # entry, or an explicit Manager/Super Administrator correction whose
    # value actually differs from what pure carry-forward derivation would
    # otherwise produce (see stock_service.upsert_daily_figure()). False
    # for every other row (an "inherited display" row a save happened to
    # touch without changing Opening, or a row a Super Administrator reset
    # to zero) — webapp.services.stock_service.daily_figure_view() ignores
    # opening_base_qty entirely on a False row and recomputes it live from
    # the nearest True anchor instead, so such a row can never permanently
    # freeze a date's Opening Stock at a stale value.
    opening_stock_is_override = db.Column(db.Boolean, nullable=False, default=False)

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
