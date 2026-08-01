"""
Stage 8 Part 2 — global, shared "how often is this product actually used"
tracking, sourced only from finalized Dispatch/Returns/Production
activity (never opened dropdowns, viewed products, drafts, failed saves,
Skip, or a zero-movement No Activity Today — see
webapp/services/product_usage_service.py).

One row per (source, source_id, product_id) — a finalized record touching
three different products creates three rows, never duplicated per line
within the same record. The unique constraint is what makes recording
usage idempotent: re-finalizing the same record (after a reopen/edit)
upserts the existing row's `used_at` rather than creating a second event,
and a genuine duplicate/retried request can never double-count.

This is a raw, timestamped event log rather than a running counter
specifically so the active-usage score (see product_usage_service.py) can
be recomputed fresh from the current date on every read — a plain
incrementing counter could never "naturally decay," since a counter never
goes down on its own.
"""
from datetime import datetime, timezone

from webapp.extensions import db

SOURCE_DISPATCH = "dispatch"
SOURCE_RETURNS = "returns"
SOURCE_PRODUCTION = "production"
SOURCES = [SOURCE_DISPATCH, SOURCE_RETURNS, SOURCE_PRODUCTION]


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class ProductUsageEvent(db.Model):
    __tablename__ = "product_usage_events"

    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"), nullable=False, index=True)
    source = db.Column(db.String(20), nullable=False)
    source_id = db.Column(db.Integer, nullable=False)
    used_at = db.Column(db.DateTime(), nullable=False, default=_utcnow, index=True)

    product = db.relationship("Product")

    __table_args__ = (
        db.UniqueConstraint("source", "source_id", "product_id", name="uq_product_usage_event_source_product"),
    )
