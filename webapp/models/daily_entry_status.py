"""
Ownership/completion tracking for one Date + Shift + Product Daily Figures
entry — deliberately a separate table from DailyFigure rather than new
columns on it: a DailyFigure row is only ever created when Opening Stock is
actually written (see stock_service.upsert_daily_figure()), but a product
whose Opening Stock was already locked from a prior period may still need
to be reviewed and marked "No Activity Today" without ever creating or
touching a DailyFigure row — see Stage 7's "do not create meaningless stock
movement rows solely because all quantity inputs are zero" requirement.

The (date, shift, product_id) unique constraint is the concurrency-safety
mechanism itself: a compare-and-swap UPDATE against `completed` (see
webapp/services/daily_entry_status_service.py) is what guarantees two
Operators can never both successfully complete the same entry, and a
plain get-or-create against this constraint is what prevents duplicate
rows for the same key even under a race on first creation.
"""
from datetime import datetime, timezone

from webapp.extensions import db

COMPLETION_TYPE_WITH_DATA = "with_data"
COMPLETION_TYPE_NO_ACTIVITY = "no_activity"
COMPLETION_TYPES = [COMPLETION_TYPE_WITH_DATA, COMPLETION_TYPE_NO_ACTIVITY]


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class DailyEntryStatus(db.Model):
    __tablename__ = "daily_entry_status"

    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.String(10), nullable=False, index=True)
    shift = db.Column(db.String(10), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"), nullable=False, index=True)

    # In-progress ownership lock — null lock_user_id means no one currently
    # holds it. Refreshed (not re-created) each time the same user re-opens
    # the entry; a stale lock (see daily_entry_status_service.LOCK_STALE_MINUTES)
    # is treated as if it were never held.
    lock_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    lock_acquired_at = db.Column(db.DateTime(), nullable=True)

    # Completion — the compare-and-swap target for concurrent-submission
    # safety (see claim_completion() in the service module). completion_type
    # distinguishes a real data submission from an explicit "No Activity
    # Today" review so a later viewer can tell the two apart.
    completed = db.Column(db.Boolean, nullable=False, default=False)
    completion_type = db.Column(db.String(20), nullable=True)
    completed_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    completed_at = db.Column(db.DateTime(), nullable=True)

    # Most recent reopen event only — full history lives in the audit log
    # (record_audit()), same as every other reopen in this app.
    reopened_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    reopened_at = db.Column(db.DateTime(), nullable=True)
    reopen_reason = db.Column(db.Text, nullable=True)

    created_at = db.Column(db.DateTime(), nullable=False, default=_utcnow)
    updated_at = db.Column(db.DateTime(), nullable=False, default=_utcnow, onupdate=_utcnow)

    product = db.relationship("Product")

    __table_args__ = (
        db.UniqueConstraint("date", "shift", "product_id", name="uq_daily_entry_status_date_shift_product"),
    )
