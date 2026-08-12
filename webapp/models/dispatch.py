from datetime import datetime, timezone

from webapp.extensions import db
from webapp.services.business_calendar import format_kampala_datetime

STATUS_DRAFT = "draft"
STATUS_FINALIZED = "finalized"
STATUS_VOID = "void"
STATUSES = [STATUS_DRAFT, STATUS_FINALIZED, STATUS_VOID]

SHIFT_DAY = "Day"
SHIFT_NIGHT = "Night"
SHIFTS = [SHIFT_DAY, SHIFT_NIGHT]


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Dispatch(db.Model):
    __tablename__ = "dispatches"

    id = db.Column(db.Integer, primary_key=True)
    dispatch_number = db.Column(db.String(40), nullable=False, index=True)
    date = db.Column(db.String(10), nullable=False, index=True)  # YYYY-MM-DD, matches legacy entries.date format
    shift = db.Column(db.String(10), nullable=False)
    customer_id = db.Column(db.Integer, db.ForeignKey("customers.id"), nullable=False, index=True)
    invoice_number = db.Column(db.String(40), nullable=True, index=True)
    notes = db.Column(db.Text, nullable=True)
    status = db.Column(db.String(20), nullable=False, default=STATUS_DRAFT, index=True)

    # The category selected at entry time (FK, for live filtering/joins) plus
    # text snapshots of both the category and customer names as they were
    # AT THAT MOMENT. Both categories and customers are mutable (rename,
    # reassign, merge) — unlike packaging_rules, which are versioned and
    # never edited in place — so only a text snapshot can guarantee a
    # historical dispatch keeps displaying what applied when it was
    # recorded, regardless of what happens to the category/customer later.
    sales_category_id = db.Column(db.Integer, db.ForeignKey("sales_categories.id"), nullable=True, index=True)
    sales_category_name_snapshot = db.Column(db.String(80), nullable=True)
    customer_name_snapshot = db.Column(db.String(160), nullable=True)

    duplicate_override = db.Column(db.Boolean, nullable=False, default=False)
    duplicate_override_reason = db.Column(db.Text, nullable=True)

    created_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    created_at = db.Column(db.DateTime(), nullable=False, default=_utcnow)
    updated_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    updated_at = db.Column(db.DateTime(), nullable=False, default=_utcnow, onupdate=_utcnow)
    finalized_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    finalized_at = db.Column(db.DateTime(), nullable=True)
    voided_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    voided_at = db.Column(db.DateTime(), nullable=True)
    void_reason = db.Column(db.Text, nullable=True)

    customer = db.relationship("Customer")
    lines = db.relationship(
        "DispatchLine", back_populates="dispatch", order_by="DispatchLine.id",
        cascade="all, delete-orphan",
    )

    def to_dict(self, include_lines=True):
        d = {
            "id": self.id,
            "dispatch_number": self.dispatch_number,
            "date": self.date,
            "shift": self.shift,
            "customer_id": self.customer_id,
            # Prefer the as-recorded snapshot (historical fidelity) — only
            # falls back to the live name for dispatches that predate this
            # enhancement and never got one.
            "customer_name": self.customer_name_snapshot or (self.customer.name if self.customer else None),
            "customer_name_snapshot": self.customer_name_snapshot,
            "sales_category_id": self.sales_category_id,
            "sales_category_name": self.sales_category_name_snapshot,
            "sales_category_name_snapshot": self.sales_category_name_snapshot,
            "invoice_number": self.invoice_number,
            "notes": self.notes,
            "status": self.status,
            "duplicate_override": self.duplicate_override,
            "duplicate_override_reason": self.duplicate_override_reason,
            "created_by": self.created_by,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            # Read-only reporting metadata — the automatic entry time a
            # Manager can use to spot an accidental duplicate entry (e.g.
            # two dispatches "created" seconds apart). Africa/Kampala,
            # formatted server-side (see business_calendar.
            # format_kampala_datetime()'s own docstring for why — never
            # the browser's local timezone). Never editable, never used
            # in any stock calculation, and never touched by a later
            # correction (created_at itself is only ever set once, at
            # insert time — see the column's own default=).
            "created_at_label": format_kampala_datetime(self.created_at),
            "updated_by": self.updated_by,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "finalized_by": self.finalized_by,
            "finalized_at": self.finalized_at.isoformat() if self.finalized_at else None,
            "voided_by": self.voided_by,
            "voided_at": self.voided_at.isoformat() if self.voided_at else None,
            "void_reason": self.void_reason,
        }
        if include_lines:
            d["lines"] = [line.to_dict() for line in self.lines]
        return d


class DispatchLine(db.Model):
    __tablename__ = "dispatch_lines"

    id = db.Column(db.Integer, primary_key=True)
    dispatch_id = db.Column(db.Integer, db.ForeignKey("dispatches.id"), nullable=False, index=True)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"), nullable=False, index=True)

    cartons = db.Column(db.Integer, nullable=False, default=0)
    packs = db.Column(db.Integer, nullable=False, default=0)
    pieces = db.Column(db.Integer, nullable=False, default=0)
    base_unit_qty = db.Column(db.Integer, nullable=False)

    # Snapshot of the rule used at entry time — never re-derived later, so a
    # subsequent packaging-rule change never reinterprets this historical row.
    packaging_rule_id = db.Column(db.Integer, db.ForeignKey("packaging_rules.id"), nullable=False)

    line_notes = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime(), nullable=False, default=_utcnow)
    updated_at = db.Column(db.DateTime(), nullable=False, default=_utcnow, onupdate=_utcnow)

    dispatch = db.relationship("Dispatch", back_populates="lines")
    product = db.relationship("Product")
    packaging_rule = db.relationship("PackagingRule")

    def to_dict(self):
        from webapp.services.quantity_format import qty_label
        return {
            "id": self.id,
            "dispatch_id": self.dispatch_id,
            "product_id": self.product_id,
            "product_name": self.product.name if self.product else None,
            "cartons": self.cartons,
            "packs": self.packs,
            "pieces": self.pieces,
            "base_unit_qty": self.base_unit_qty,
            "quantity_label": qty_label(self.cartons, self.packs, self.pieces, self.packaging_rule),
            "packaging_rule_id": self.packaging_rule_id,
            "line_notes": self.line_notes,
        }
