from datetime import datetime, timezone

from webapp.extensions import db


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class SparePartFabrication(db.Model):
    """
    History of fabricating a spare part in-house: what it cost to have
    fabricated and what it was sold to the factory for, on a given date.
    One SparePart may have MANY fabrication records over time (price
    changes per batch) — this is deliberately its own history table, never
    a single price column on SparePart.

    FINANCIAL DATA — Super Administrator only end to end (see
    webapp/routes/spare_part_fabrications.py); never exposed through any
    ordinary Spare Parts API, Dashboard, Stock In/Out, or History route.

    image_path is always a server-generated filename (uuid4 + an
    extension derived from the actual validated image bytes — see
    spare_part_fabrication_service.py), stored under DB_PATH's own
    uploads directory, never the client's original filename — this is
    what prevents path traversal and content-type spoofing. It is never
    returned to the API directly (see to_dict's has_image); the image is
    only ever served through the authenticated image route.
    """
    __tablename__ = "spare_part_fabrications"

    id = db.Column(db.Integer, primary_key=True)
    spare_part_id = db.Column(db.Integer, db.ForeignKey("spare_parts.id"), nullable=False, index=True)
    fabrication_date = db.Column(db.String(10), nullable=False, index=True)  # YYYY-MM-DD business date
    fabrication_cost_ugx = db.Column(db.Integer, nullable=False)
    sold_to_factory_ugx = db.Column(db.Integer, nullable=False)
    image_path = db.Column(db.String(120), nullable=True)
    notes = db.Column(db.Text, nullable=True)
    created_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    created_at = db.Column(db.DateTime(), nullable=False, default=_utcnow)
    updated_at = db.Column(db.DateTime(), nullable=False, default=_utcnow, onupdate=_utcnow)

    @property
    def profit_loss_ugx(self):
        """sold_to_factory_ugx - fabrication_cost_ugx. Always server-derived
        from the two stored amounts — never stored as its own editable
        column, and never trusted from client input (see the service's
        create/update, which ignore any client-sent profit figure)."""
        return self.sold_to_factory_ugx - self.fabrication_cost_ugx

    def to_dict(self):
        return {
            "id": self.id,
            "spare_part_id": self.spare_part_id,
            "fabrication_date": self.fabrication_date,
            "fabrication_cost_ugx": self.fabrication_cost_ugx,
            "sold_to_factory_ugx": self.sold_to_factory_ugx,
            "profit_loss_ugx": self.profit_loss_ugx,
            "has_image": self.image_path is not None,
            "notes": self.notes,
            "created_by_user_id": self.created_by_user_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
