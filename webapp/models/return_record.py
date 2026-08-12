"""
Returns Book: stock physically returned unused (by a customer/truck/field
representative), recorded here before it ever reaches Daily Figures. Only
finalized returns contribute to Daily Figures — see
webapp/services/stock_service.py's return_base_qty(). Mirrors
webapp/models/dispatch.py's Dispatch/DispatchLine pattern (draft/finalized/
void lifecycle, per-line packaging-rule snapshot) so the two books behave
identically from an operator's point of view.

Deliberately has no `shift` column: the physical Returns Book is a day-only
workflow (see the Stage 5 spec's shift rules) — every return contributes
only to that date's Day Daily Figures, never Night.
"""
from datetime import datetime, timezone

from webapp.extensions import db
from webapp.services.business_calendar import format_kampala_datetime

STATUS_DRAFT = "draft"
STATUS_FINALIZED = "finalized"
STATUS_VOID = "void"
STATUSES = [STATUS_DRAFT, STATUS_FINALIZED, STATUS_VOID]


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class ReturnRecord(db.Model):
    __tablename__ = "return_records"

    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.String(10), nullable=False, index=True)  # YYYY-MM-DD

    # "Returned by" — reuses the existing Customer/recipient master data
    # (a customer, truck route, or field representative is recorded as a
    # Customer exactly like a dispatch recipient) rather than duplicating a
    # second name-lookup table. A text snapshot is kept too, for the same
    # historical-fidelity reason Dispatch snapshots customer_name: customers
    # can be renamed or merged later.
    returned_by_customer_id = db.Column(db.Integer, db.ForeignKey("customers.id"), nullable=True, index=True)
    returned_by_name_snapshot = db.Column(db.String(160), nullable=True)

    # Digital stand-in for the physical book's "Name and sign" column — a
    # typed name recorded at entry time, not an image/drawn signature.
    signed_by_name = db.Column(db.String(160), nullable=True)

    received_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    verified_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)

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

    returned_by_customer = db.relationship("Customer")
    # Explicit foreign_keys since ReturnRecord has three separate FKs into
    # users.id (created_by/updated_by are read by id only, elsewhere) — these
    # two are surfaced by username in to_dict() so the Returns pages can show
    # "who received/verified this" without needing the super_admin-only
    # /api/admin/users listing endpoint.
    received_by_user = db.relationship("User", foreign_keys=[received_by])
    verified_by_user = db.relationship("User", foreign_keys=[verified_by])
    lines = db.relationship(
        "ReturnLine", back_populates="return_record", order_by="ReturnLine.id",
        cascade="all, delete-orphan",
    )

    def to_dict(self, include_lines=True):
        d = {
            "id": self.id,
            "date": self.date,
            "returned_by_customer_id": self.returned_by_customer_id,
            "returned_by_name": self.returned_by_name_snapshot or (
                self.returned_by_customer.name if self.returned_by_customer else None
            ),
            "signed_by_name": self.signed_by_name,
            "received_by": self.received_by,
            "received_by_username": self.received_by_user.username if self.received_by_user else None,
            "verified_by": self.verified_by,
            "verified_by_username": self.verified_by_user.username if self.verified_by_user else None,
            "remarks": self.remarks,
            "status": self.status,
            "created_by": self.created_by,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            # Read-only reporting metadata — see dispatch.py's identical
            # field for the full rationale (automatic entry time, Africa/
            # Kampala, never editable/stock-affecting/overwritten).
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


class ReturnLine(db.Model):
    __tablename__ = "return_lines"

    id = db.Column(db.Integer, primary_key=True)
    return_id = db.Column(db.Integer, db.ForeignKey("return_records.id"), nullable=False, index=True)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"), nullable=False, index=True)

    cartons = db.Column(db.Integer, nullable=False, default=0)
    packs = db.Column(db.Integer, nullable=False, default=0)
    pieces = db.Column(db.Integer, nullable=False, default=0)
    base_unit_qty = db.Column(db.Integer, nullable=False)

    # Snapshot of the rule used at entry time — never re-derived later, same
    # historical-fidelity rule as DispatchLine.packaging_rule_id.
    packaging_rule_id = db.Column(db.Integer, db.ForeignKey("packaging_rules.id"), nullable=False)

    line_notes = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime(), nullable=False, default=_utcnow)
    updated_at = db.Column(db.DateTime(), nullable=False, default=_utcnow, onupdate=_utcnow)

    return_record = db.relationship("ReturnRecord", back_populates="lines")
    product = db.relationship("Product")
    packaging_rule = db.relationship("PackagingRule")

    def to_dict(self):
        from webapp.services.quantity_format import qty_label
        return {
            "id": self.id,
            "return_id": self.return_id,
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
