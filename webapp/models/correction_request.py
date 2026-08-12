"""
Historical correction/void requests — the controlled workflow an Operator
must use once a record's own business Date is no longer today (Africa/
Kampala; see webapp/services/business_calendar.py and
record_correction_service.operator_can_directly_edit()). A request never
mutates the underlying Dispatch/Return/Production record itself; approving
one simply invokes the SAME existing correct_record()/void_*() functions
Manager/Super Admin already use directly — this table is purely an
approval queue in front of that existing, already-audited machinery, never
a second/competing correction engine.
"""
from datetime import datetime, timezone

from webapp.extensions import db

ACTION_CORRECT = "correct"
ACTION_VOID = "void"
ACTIONS = [ACTION_CORRECT, ACTION_VOID]

STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
STATUSES = [STATUS_PENDING, STATUS_APPROVED, STATUS_REJECTED]

RECORD_TYPES = ["dispatch", "returns", "production"]


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class CorrectionRequest(db.Model):
    __tablename__ = "correction_requests"

    id = db.Column(db.Integer, primary_key=True)
    record_type = db.Column(db.String(20), nullable=False, index=True)
    record_id = db.Column(db.Integer, nullable=False, index=True)
    action = db.Column(db.String(20), nullable=False)

    requested_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    reason = db.Column(db.Text, nullable=False)
    # For action=correct: the exact same field shape correct_record()
    # already accepts (lines/notes/date/customer_id/... — whatever this
    # source_type supports), applied unchanged on approval. For
    # action=void: always null — void takes only a reason.
    payload_json = db.Column(db.Text, nullable=True)
    # A full snapshot of the record as it stood at request time — for
    # display/audit only, never read back into the approval itself (the
    # approval always re-reads the CURRENT record, so a second edit that
    # lands between request and review is never silently overwritten).
    before_snapshot_json = db.Column(db.Text, nullable=False)

    status = db.Column(db.String(20), nullable=False, default=STATUS_PENDING, index=True)
    reviewed_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    reviewed_at = db.Column(db.DateTime(), nullable=True)
    review_note = db.Column(db.Text, nullable=True)

    created_at = db.Column(db.DateTime(), nullable=False, default=_utcnow, index=True)

    requester = db.relationship("User", foreign_keys=[requested_by])
    reviewer = db.relationship("User", foreign_keys=[reviewed_by])

    def to_dict(self):
        import json
        return {
            "id": self.id,
            "record_type": self.record_type,
            "record_id": self.record_id,
            "action": self.action,
            "requested_by": self.requested_by,
            "requested_by_username": self.requester.username if self.requester else None,
            "reason": self.reason,
            "payload": json.loads(self.payload_json) if self.payload_json else None,
            "before_snapshot": json.loads(self.before_snapshot_json) if self.before_snapshot_json else None,
            "status": self.status,
            "reviewed_by": self.reviewed_by,
            "reviewed_by_username": self.reviewer.username if self.reviewer else None,
            "reviewed_at": self.reviewed_at.isoformat() if self.reviewed_at else None,
            "review_note": self.review_note,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
