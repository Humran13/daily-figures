"""
Final pre-deployment correction — Manager/Super Administrator Daily
Figures review-and-submit workflow. Deliberately separate from
DailyEntryStatus (Stage 7's per-product Operator ownership/completion
tracking, unchanged and untouched by this feature): that table answers
"has an Operator submitted or confirmed No Activity for this one
product?"; these two tables answer a completely different question — "is
there a Manager/Super Administrator review in progress or submitted for
this whole Date + Shift, and which products has THAT reviewer actually
looked at?" A product can be Operator-completed and still awaiting
Manager review, or vice versa (a Manager doing the very first entry on a
product no Operator has touched yet) — the two tracks are independent by
design (see webapp/services/daily_review_service.py).

DailyReviewSession is the one active review identity for a Date + Shift
(enforced by its own unique constraint, mirroring DailyEntryStatus's
established pattern) — metadata only. It never stores a stock total,
never replaces a DailyFigure row, and never creates source-book movement;
submitting or reopening it only ever changes this table's own status
fields, nothing else.

DailyReviewProductState is the per-product "has the current reviewer
looked at this one yet" marker within one review session — "reviewed" or
"skipped" only; the absence of a row for a product means "not yet
reviewed". This is what lets final submission be blocked while any
eligible product remains unvisited or skipped, without needing to infer
that from DailyFigure/DailyEntryStatus rows that were never designed to
answer that question.
"""
from datetime import datetime, timezone

from webapp.extensions import db

REVIEW_STATUS_IN_PROGRESS = "in_progress"
REVIEW_STATUS_SUBMITTED = "submitted"
REVIEW_STATUS_REOPENED = "reopened"
REVIEW_STATUSES = [REVIEW_STATUS_IN_PROGRESS, REVIEW_STATUS_SUBMITTED, REVIEW_STATUS_REOPENED]

REVIEW_PRODUCT_STATE_REVIEWED = "reviewed"      # visited, nothing changed
REVIEW_PRODUCT_STATE_EDITED = "edited"          # visited, a correction was saved
REVIEW_PRODUCT_STATE_SKIPPED = "skipped"        # explicitly deferred, does not count as reviewed
REVIEW_PRODUCT_STATES = [REVIEW_PRODUCT_STATE_REVIEWED, REVIEW_PRODUCT_STATE_EDITED, REVIEW_PRODUCT_STATE_SKIPPED]
# Both count toward "every eligible product has been reviewed" for final
# submission purposes — only SKIPPED (or no row at all) blocks it.
REVIEW_PRODUCT_STATES_COUNTED_AS_REVIEWED = (REVIEW_PRODUCT_STATE_REVIEWED, REVIEW_PRODUCT_STATE_EDITED)


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class DailyReviewSession(db.Model):
    __tablename__ = "daily_review_sessions"

    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.String(10), nullable=False, index=True)
    shift = db.Column(db.String(10), nullable=False)

    status = db.Column(db.String(20), nullable=False, default=REVIEW_STATUS_IN_PROGRESS)

    started_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    started_role = db.Column(db.String(20), nullable=True)
    started_at = db.Column(db.DateTime(), nullable=False, default=_utcnow)

    submitted_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    submitted_role = db.Column(db.String(20), nullable=True)
    submitted_at = db.Column(db.DateTime(), nullable=True)

    # Most recent reopen event only — full history lives in the audit log
    # (record_audit()), exactly like DailyEntryStatus.reopened_* already
    # does; the prior submission itself is never erased from AuditLog.
    reopened_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    reopened_at = db.Column(db.DateTime(), nullable=True)
    reopen_reason = db.Column(db.Text, nullable=True)

    created_at = db.Column(db.DateTime(), nullable=False, default=_utcnow)
    # Doubles as the concurrency token for this session's own status —
    # bumped on every status transition via onupdate.
    updated_at = db.Column(db.DateTime(), nullable=False, default=_utcnow, onupdate=_utcnow)

    __table_args__ = (
        db.UniqueConstraint("date", "shift", name="uq_daily_review_session_date_shift"),
    )

    def to_dict(self):
        return {
            "id": self.id,
            "date": self.date,
            "shift": self.shift,
            "status": self.status,
            "started_by": self.started_by,
            "started_role": self.started_role,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "submitted_by": self.submitted_by,
            "submitted_role": self.submitted_role,
            "submitted_at": self.submitted_at.isoformat() if self.submitted_at else None,
            "reopened_by": self.reopened_by,
            "reopened_at": self.reopened_at.isoformat() if self.reopened_at else None,
            "reopen_reason": self.reopen_reason,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class DailyReviewProductState(db.Model):
    __tablename__ = "daily_review_product_states"

    id = db.Column(db.Integer, primary_key=True)
    review_session_id = db.Column(db.Integer, db.ForeignKey("daily_review_sessions.id"), nullable=False, index=True)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"), nullable=False, index=True)

    state = db.Column(db.String(20), nullable=False)
    # Required (enforced at submission time, not at the moment of editing —
    # see webapp/services/daily_review_service.py's build_summary()) when
    # state is "edited": why an existing manual value was changed. Never
    # required for "reviewed" (nothing changed) or "skipped".
    reason = db.Column(db.Text, nullable=True)
    updated_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    updated_at = db.Column(db.DateTime(), nullable=False, default=_utcnow, onupdate=_utcnow)
    # Safety correction — a snapshot of DailyFigure.updated_at AS SEEN AT
    # THE MOMENT this product was marked reviewed/edited (None if no
    # DailyFigure row existed yet). Compared against the row's CURRENT
    # updated_at on every build_summary()/submit_review() call: a mismatch
    # means Opening Stock, notes, or provenance changed after this review
    # (by this reviewer or a concurrent one) and the product must be
    # looked at again before submission — independent of, and in addition
    # to, the existing Dispatch/Returns/Production "source touched since"
    # check, which this column does not replace.
    daily_figure_updated_at = db.Column(db.DateTime(), nullable=True)

    __table_args__ = (
        db.UniqueConstraint("review_session_id", "product_id", name="uq_daily_review_product_state"),
    )
