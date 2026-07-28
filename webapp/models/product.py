from datetime import datetime, timezone

from webapp.extensions import db


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Product(db.Model):
    __tablename__ = "products"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), unique=True, nullable=False, index=True)
    display_order = db.Column(db.Integer, nullable=False, default=0)
    active = db.Column(db.Boolean, nullable=False, default=True)
    parent_product_id = db.Column(db.Integer, db.ForeignKey("products.id"), nullable=True)
    # Base-unit (pieces) threshold for the dashboard's low-stock warning.
    # Null means "no warning configured" — not every product needs one.
    low_stock_threshold = db.Column(db.Integer, nullable=True)
    created_at = db.Column(db.DateTime(), nullable=False, default=_utcnow)
    updated_at = db.Column(db.DateTime(), nullable=False, default=_utcnow, onupdate=_utcnow)

    parent = db.relationship("Product", remote_side=[id])
    packaging_rules = db.relationship(
        "PackagingRule", back_populates="product", order_by="PackagingRule.effective_from"
    )

    def current_packaging_rule(self, at=None):
        at = at or _utcnow()
        for rule in sorted(self.packaging_rules, key=lambda r: r.effective_from, reverse=True):
            starts = rule.effective_from
            ends = rule.effective_to
            if starts <= at and (ends is None or at < ends):
                return rule
        return None

    def to_dict(self):
        rule = self.current_packaging_rule()
        return {
            "id": self.id,
            "name": self.name,
            "display_order": self.display_order,
            "active": self.active,
            "parent_product_id": self.parent_product_id,
            "low_stock_threshold": self.low_stock_threshold,
            "packaging_rule": rule.to_dict() if rule else None,
        }
