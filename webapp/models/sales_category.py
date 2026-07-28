from datetime import datetime, timezone

from webapp.extensions import db


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class SalesCategory(db.Model):
    """
    Sales channel a recipient belongs to (Corporate/Metro/Upcountry/
    Shop-Kikuubo/Factory Sales) — independent of Customer.category
    (customer/salesperson/other), which answers a different question.
    """
    __tablename__ = "sales_categories"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), unique=True, nullable=False, index=True)
    active = db.Column(db.Boolean, nullable=False, default=True)
    display_order = db.Column(db.Integer, nullable=False, default=0)
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    created_at = db.Column(db.DateTime(), nullable=False, default=_utcnow)
    updated_at = db.Column(db.DateTime(), nullable=False, default=_utcnow, onupdate=_utcnow)

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "active": self.active,
            "display_order": self.display_order,
        }
