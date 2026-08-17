"""
Historical correction requests — the controlled workflow an Operator must
use once their direct 24-hour Edit window (record.created_at + 24h; see
record_correction_service.operator_can_directly_edit()) has closed. A
request never mutates the underlying Dispatch/Return/Production record on
creation. Approving one does NOT immediately apply anything either — it
starts a one-time, record-specific, 24-hour Edit GRANT for the requesting
Operator (STATUS_APPROVED IS the "grant active" state; reviewed_at is the
grant's start instant). The Operator then submits their correction
themselves through the exact same /correct route and UI a same-window
direct edit already uses — this table has never been a second/competing
correction engine, and still isn't: it is purely an approval queue in
front of that one existing, already-audited record_correction_service.
correct_record().

Void has NO request path at all — an Operator may Void their own record
directly regardless of age (see record_correction_service.
operator_can_directly_void()), so ACTION_VOID exists only for backward
compatibility with any request created before this round; creating a new
one is refused by correction_request_service.create_request().
"""
from datetime import datetime, timezone

from webapp.extensions import db

ACTION_CORRECT = "correct"
ACTION_VOID = "void"  # retained for backward compatibility only — see module docstring
ACTIONS = [ACTION_CORRECT, ACTION_VOID]

STATUS_PENDING = "pending"
# STATUS_APPROVED means "an active, unconsumed 24-hour edit grant exists"
# for action=correct requests (see module docstring) — historically it
# meant "immediately applied" before this round; that old meaning no
# longer applies to any NEWLY approved request.
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
# New this round: the two ways an approved grant's lifecycle ends.
STATUS_COMPLETED = "completed"  # the one permitted correction was successfully submitted and consumed the grant
STATUS_EXPIRED = "expired"      # the 24-hour grant window closed with the grant never used
STATUSES = [STATUS_PENDING, STATUS_APPROVED, STATUS_REJECTED, STATUS_COMPLETED, STATUS_EXPIRED]

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
    # Doubles as the edit grant's START instant once status becomes
    # STATUS_APPROVED (action=correct) — the grant expires exactly
    # GRANT_WINDOW after this timestamp; see correction_request_service.py.
    reviewed_at = db.Column(db.DateTime(), nullable=True)
    review_note = db.Column(db.Text, nullable=True)
    # New this round: when the one permitted correction was successfully
    # submitted (STATUS_COMPLETED) — never set for any other status.
    completed_at = db.Column(db.DateTime(), nullable=True)

    created_at = db.Column(db.DateTime(), nullable=False, default=_utcnow, index=True)

    requester = db.relationship("User", foreign_keys=[requested_by])
    reviewer = db.relationship("User", foreign_keys=[reviewed_by])

    def grant_expires_at(self):
        """The instant this request's edit grant closes, or None if this
        request was never approved (action=correct only — see
        correction_request_service.GRANT_WINDOW)."""
        from webapp.services.correction_request_service import GRANT_WINDOW
        if self.reviewed_at is None:
            return None
        return self.reviewed_at + GRANT_WINDOW

    def to_dict(self):
        import json
        from webapp.services.business_calendar import format_kampala_datetime
        expires_at = self.grant_expires_at()
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
            "reviewed_at_label": format_kampala_datetime(self.reviewed_at),
            "review_note": self.review_note,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "completed_at_label": format_kampala_datetime(self.completed_at),
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "created_at_label": format_kampala_datetime(self.created_at),
            "grant_expires_at": expires_at.isoformat() if expires_at else None,
            "grant_expires_at_label": format_kampala_datetime(expires_at),
        }
