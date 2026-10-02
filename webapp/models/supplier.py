from datetime import datetime, timezone

from webapp.extensions import db


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Supplier(db.Model):
    """
    Where/how a spare part can be sourced — never stock ownership. Dedup
    identity for import purposes is normalized (name, building); the same
    supplier may quote different prices for different spares over time
    (see SparePartSupplier).
    """
    __tablename__ = "spare_suppliers"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(160), nullable=False)
    building = db.Column(db.Text, nullable=True)
    contacts = db.Column(db.Text, nullable=True)
    notes = db.Column(db.Text, nullable=True)
    active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime(), nullable=False, default=_utcnow)
    updated_at = db.Column(db.DateTime(), nullable=False, default=_utcnow, onupdate=_utcnow)

    def to_dict(self):
        return {
            "id": self.id, "name": self.name, "building": self.building,
            "contacts": self.contacts, "notes": self.notes, "active": self.active,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
