import re
from datetime import datetime, timezone

from sqlalchemy.orm import validates

from webapp.extensions import db

CATEGORY_CUSTOMER = "customer"
CATEGORY_SALESPERSON = "salesperson"
CATEGORY_OTHER = "other"
CATEGORIES = [CATEGORY_CUSTOMER, CATEGORY_SALESPERSON, CATEGORY_OTHER]


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def normalize_name(name):
    """Case-insensitive, whitespace-collapsed comparison key — never used
    for display, only for duplicate detection (exact display spelling in
    `name` is always preserved)."""
    return re.sub(r"\s+", " ", (name or "").strip()).lower()


class Customer(db.Model):
    __tablename__ = "customers"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(160), nullable=False, index=True)
    # Auto-maintained by the validator below whenever `name` is set — never
    # assign this directly. Used for case/whitespace-insensitive duplicate
    # detection across ALL customers (active, inactive, temporary, and
    # merged-away), backed by a unique index (see migration) so a race
    # between two concurrent imports can't slip a duplicate past the
    # in-Python pre-check.
    normalized_name = db.Column(db.String(160), nullable=True, index=True)
    # Entity-type flag — independent of sales_category_id, which answers a
    # different question ("which sales channel"). Unchanged from Phase 2.
    category = db.Column(db.String(20), nullable=False, default=CATEGORY_CUSTOMER)
    active = db.Column(db.Boolean, nullable=False, default=True)
    contact_info = db.Column(db.String(255), nullable=True)
    notes = db.Column(db.Text, nullable=True)

    sales_category_id = db.Column(db.Integer, db.ForeignKey("sales_categories.id"), nullable=True, index=True)
    # Operator-entered "Other / New Customer" recipient awaiting admin review.
    is_temporary = db.Column(db.Boolean, nullable=False, default=False, index=True)
    # Set when this record is merged into another (the surviving, canonical
    # one). The row itself is never deleted — dispatches that reference it
    # keep working, and history keeps its original customer_name_snapshot.
    merged_into_id = db.Column(db.Integer, db.ForeignKey("customers.id"), nullable=True, index=True)

    created_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    created_at = db.Column(db.DateTime(), nullable=False, default=_utcnow)
    updated_at = db.Column(db.DateTime(), nullable=False, default=_utcnow, onupdate=_utcnow)

    sales_category = db.relationship("SalesCategory")
    merged_into = db.relationship("Customer", remote_side=[id])

    @validates("name")
    def _set_normalized_name(self, key, value):
        self.normalized_name = normalize_name(value)
        return value

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "category": self.category,
            "active": self.active,
            "contact_info": self.contact_info,
            "notes": self.notes,
            "sales_category_id": self.sales_category_id,
            "sales_category_name": self.sales_category.name if self.sales_category else None,
            "is_temporary": self.is_temporary,
            "merged_into_id": self.merged_into_id,
        }
