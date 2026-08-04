"""
Final stock architecture — clean ledger cutover.

A LedgerCutover establishes a new, verified operational starting point for
every active product's Opening Stock, without deleting or rewriting a
single historical record. It never derives its balances from the
pre-cutover history (which may contain a mixture of legacy-migrated,
reset-created, and incorrectly-promoted figures) — every balance comes
from an explicitly entered, verified physical count.

Workflow: draft -> verified -> activated (or -> cancelled from either of
the first two states). Only an ACTIVATED cutover ever affects live stock
math — see webapp/services/ledger_cutover_service.py and
webapp/services/stock_service.py's OPENING_STOCK_SOURCE_LEDGER_CUTOVER.
Draft/verified/cancelled cutovers are inert: their balances exist only as
LedgerCutoverBalance rows, never written onto a DailyFigure until
activation.

Once activated, at most one OTHER cutover may also be activated (a later
cutover activated at a LATER effective Date + Shift takes over from its
own point forward) — but a cutover is never silently superseded; see
ledger_cutover_service.activate_cutover()'s guard against an overlapping
already-active cutover.
"""
from datetime import datetime, timezone

from webapp.extensions import db

STATUS_DRAFT = "draft"
STATUS_VERIFIED = "verified"
STATUS_ACTIVATED = "activated"
STATUS_CANCELLED = "cancelled"
STATUSES = [STATUS_DRAFT, STATUS_VERIFIED, STATUS_ACTIVATED, STATUS_CANCELLED]


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class LedgerCutover(db.Model):
    __tablename__ = "ledger_cutovers"

    id = db.Column(db.Integer, primary_key=True)
    effective_date = db.Column(db.String(10), nullable=False, index=True)
    effective_shift = db.Column(db.String(10), nullable=False)

    status = db.Column(db.String(20), nullable=False, default=STATUS_DRAFT)
    reason = db.Column(db.Text, nullable=True)

    created_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    created_at = db.Column(db.DateTime(), nullable=False, default=_utcnow)

    verified_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    verified_at = db.Column(db.DateTime(), nullable=True)

    activated_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    activated_at = db.Column(db.DateTime(), nullable=True)

    cancelled_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    cancelled_at = db.Column(db.DateTime(), nullable=True)

    # Doubles as the concurrency/preview-staleness token for this cutover's
    # own status+balance set — bumped on every mutation via onupdate,
    # exactly like DailyReviewSession.updated_at. Independent of (and in
    # addition to) the SHA256 preview-token mechanism computed at preview
    # time from the exact set of balances (see ledger_cutover_service.py).
    updated_at = db.Column(db.DateTime(), nullable=False, default=_utcnow, onupdate=_utcnow)

    balances = db.relationship(
        "LedgerCutoverBalance", backref="cutover", cascade="all, delete-orphan",
        order_by="LedgerCutoverBalance.product_id",
    )

    def to_dict(self, include_balances=False):
        d = {
            "id": self.id,
            "effective_date": self.effective_date,
            "effective_shift": self.effective_shift,
            "status": self.status,
            "reason": self.reason,
            "created_by": self.created_by,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "verified_by": self.verified_by,
            "verified_at": self.verified_at.isoformat() if self.verified_at else None,
            "activated_by": self.activated_by,
            "activated_at": self.activated_at.isoformat() if self.activated_at else None,
            "cancelled_by": self.cancelled_by,
            "cancelled_at": self.cancelled_at.isoformat() if self.cancelled_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
        if include_balances:
            d["balances"] = [b.to_dict() for b in self.balances]
        return d


class LedgerCutoverBalance(db.Model):
    """One product's verified physical-count Opening Stock for a cutover —
    never derived from pre-cutover DailyFigure history. Exact integer base
    units, converted immediately from the entered cartons/packs/pieces via
    the product's own packaging rule at entry time (never re-interpreted
    later, even if the packaging rule subsequently changes)."""
    __tablename__ = "ledger_cutover_balances"

    id = db.Column(db.Integer, primary_key=True)
    cutover_id = db.Column(db.Integer, db.ForeignKey("ledger_cutovers.id"), nullable=False, index=True)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"), nullable=False, index=True)

    cartons = db.Column(db.Integer, nullable=False, default=0)
    packs = db.Column(db.Integer, nullable=False, default=0)
    pieces = db.Column(db.Integer, nullable=False, default=0)
    base_qty = db.Column(db.Integer, nullable=False, default=0)

    created_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    created_at = db.Column(db.DateTime(), nullable=False, default=_utcnow)
    updated_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    updated_at = db.Column(db.DateTime(), nullable=False, default=_utcnow, onupdate=_utcnow)

    product = db.relationship("Product")

    __table_args__ = (
        db.UniqueConstraint("cutover_id", "product_id", name="uq_ledger_cutover_balance_product"),
    )

    def to_dict(self):
        return {
            "id": self.id,
            "cutover_id": self.cutover_id,
            "product_id": self.product_id,
            "product_name": self.product.name if self.product else None,
            "cartons": self.cartons,
            "packs": self.packs,
            "pieces": self.pieces,
            "base_qty": self.base_qty,
            "created_by": self.created_by,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_by": self.updated_by,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
