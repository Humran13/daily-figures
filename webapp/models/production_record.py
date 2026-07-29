"""
Production Book: stock made this shift, recorded here before it ever
reaches Daily Figures. Only finalized production contributes to Daily
Figures — see webapp/services/stock_service.py's production_base_qty().
Mirrors webapp/models/dispatch.py's Dispatch/DispatchLine pattern
(draft/finalized/void lifecycle, per-line packaging-rule snapshot).

Unlike Returns, `shift` is mandatory here (Day or Night) — Production is a
genuinely shift-based workflow (see the Stage 5 spec's shift rules): a Day
production record contributes to that date's Day Daily Figures, a Night
one to Night. There is no separate "Night Returns"/"Night Dispatch"
workflow — only Production varies by shift.
"""
from datetime import datetime, timezone

from webapp.extensions import db
from webapp.models.dispatch import SHIFTS  # Day/Night — the one shift vocabulary in the app

STATUS_DRAFT = "draft"
STATUS_FINALIZED = "finalized"
STATUS_VOID = "void"
STATUSES = [STATUS_DRAFT, STATUS_FINALIZED, STATUS_VOID]


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class ProductionRecord(db.Model):
    __tablename__ = "production_records"

    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.String(10), nullable=False, index=True)  # YYYY-MM-DD
    shift = db.Column(db.String(10), nullable=False)

    remarks = db.Column(db.Text, nullable=True)
    status = db.Column(db.String(20), nullable=False, default=STATUS_DRAFT, index=True)

    created_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    created_at = db.Column(db.DateTime(), nullable=False, default=_utcnow)
    updated_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    updated_at = db.Column(db.DateTime(), nullable=False, default=_utcnow, onupdate=_utcnow)
    finalized_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    finalized_at = db.Column(db.DateTime(), nullable=True)
    voided_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    voided_at = db.Column(db.DateTime(), nullable=True)
    void_reason = db.Column(db.Text, nullable=True)

    lines = db.relationship(
        "ProductionLine", back_populates="production_record", order_by="ProductionLine.id",
        cascade="all, delete-orphan",
    )

    def to_dict(self, include_lines=True):
        d = {
            "id": self.id,
            "date": self.date,
            "shift": self.shift,
            "remarks": self.remarks,
            "status": self.status,
            "created_by": self.created_by,
            "created_at": self.created_at.isoformat() if self.created_at else None,
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


class ProductionLine(db.Model):
    __tablename__ = "production_lines"

    id = db.Column(db.Integer, primary_key=True)
    production_id = db.Column(db.Integer, db.ForeignKey("production_records.id"), nullable=False, index=True)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"), nullable=False, index=True)

    cartons = db.Column(db.Integer, nullable=False, default=0)
    packs = db.Column(db.Integer, nullable=False, default=0)
    pieces = db.Column(db.Integer, nullable=False, default=0)
    base_unit_qty = db.Column(db.Integer, nullable=False)

    packaging_rule_id = db.Column(db.Integer, db.ForeignKey("packaging_rules.id"), nullable=False)

    line_notes = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime(), nullable=False, default=_utcnow)
    updated_at = db.Column(db.DateTime(), nullable=False, default=_utcnow, onupdate=_utcnow)

    production_record = db.relationship("ProductionRecord", back_populates="lines")
    product = db.relationship("Product")
    packaging_rule = db.relationship("PackagingRule")

    def to_dict(self):
        return {
            "id": self.id,
            "production_id": self.production_id,
            "product_id": self.product_id,
            "product_name": self.product.name if self.product else None,
            "cartons": self.cartons,
            "packs": self.packs,
            "pieces": self.pieces,
            "base_unit_qty": self.base_unit_qty,
            "packaging_rule_id": self.packaging_rule_id,
            "line_notes": self.line_notes,
        }
