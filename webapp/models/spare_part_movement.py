from datetime import datetime, timezone

from webapp.extensions import db

MOVEMENT_STOCK_IN = "STOCK_IN"
MOVEMENT_STOCK_OUT = "STOCK_OUT"
MOVEMENT_ADJUSTMENT_IN = "ADJUSTMENT_IN"
MOVEMENT_ADJUSTMENT_OUT = "ADJUSTMENT_OUT"
MOVEMENT_TYPES = [MOVEMENT_STOCK_IN, MOVEMENT_STOCK_OUT, MOVEMENT_ADJUSTMENT_IN, MOVEMENT_ADJUSTMENT_OUT]
INCREASING_TYPES = (MOVEMENT_STOCK_IN, MOVEMENT_ADJUSTMENT_IN)
DECREASING_TYPES = (MOVEMENT_STOCK_OUT, MOVEMENT_ADJUSTMENT_OUT)

STATUS_ACTIVE = "active"
STATUS_VOID = "void"

REASON_OPENING_BALANCE = "opening_balance"


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class SparePartMovement(db.Model):
    """
    The authoritative Spare Parts stock ledger — mirrors Dispatch/Return/
    Production's status + voided_by/voided_at/void_reason pattern
    (webapp/services/dispatch_service.py's void_dispatch()), simplified to
    two states since a movement is one atomic entry, not a multi-line
    document needing a draft stage. Current stock is always DERIVED from
    this table (see spare_part_movement_service.current_stock()) — never
    hand-edited.
    """
    __tablename__ = "spare_part_movements"

    id = db.Column(db.Integer, primary_key=True)
    business_date = db.Column(db.String(10), nullable=False, index=True)  # YYYY-MM-DD, matches dispatch.date
    spare_part_id = db.Column(db.Integer, db.ForeignKey("spare_parts.id"), nullable=False, index=True)
    movement_type = db.Column(db.String(20), nullable=False, index=True)
    quantity = db.Column(db.Numeric(12, 3), nullable=False)  # always positive magnitude; direction = movement_type
    unit_snapshot = db.Column(db.String(20), nullable=True)
    supplier_id = db.Column(db.Integer, db.ForeignKey("spare_suppliers.id"), nullable=True)
    reference_doc = db.Column(db.String(120), nullable=True)
    requested_by = db.Column(db.String(120), nullable=True)
    approved_by = db.Column(db.String(120), nullable=True)
    reason = db.Column(db.String(60), nullable=True)
    remarks = db.Column(db.Text, nullable=True)
    status = db.Column(db.String(10), nullable=False, default=STATUS_ACTIVE, index=True)
    voided_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    voided_at = db.Column(db.DateTime(), nullable=True)
    void_reason = db.Column(db.Text, nullable=True)
    entered_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    created_at = db.Column(db.DateTime(), nullable=False, default=_utcnow)
    updated_at = db.Column(db.DateTime(), nullable=False, default=_utcnow, onupdate=_utcnow)

    def to_dict(self):
        return {
            "id": self.id,
            "reference": f"SPM-{self.id:06d}",
            "business_date": self.business_date,
            "spare_part_id": self.spare_part_id,
            "movement_type": self.movement_type,
            "quantity": str(self.quantity),
            "unit_snapshot": self.unit_snapshot,
            "supplier_id": self.supplier_id,
            "reference_doc": self.reference_doc,
            "requested_by": self.requested_by,
            "approved_by": self.approved_by,
            "reason": self.reason,
            "remarks": self.remarks,
            "status": self.status,
            "voided_by": self.voided_by,
            "voided_at": self.voided_at.isoformat() if self.voided_at else None,
            "void_reason": self.void_reason,
            "entered_by_user_id": self.entered_by_user_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
