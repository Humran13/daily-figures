from datetime import datetime, timezone

from webapp.extensions import db

UNITS = ["pcs", "rolls", "metres", "sets", "litres", "other"]


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class SparePart(db.Model):
    """
    Spare Part MASTER — describes WHAT the spare is, never what happened
    to its stock (see SparePartMovement for the authoritative ledger).
    `current_stock_cache` is an internal, denormalized concurrency gate
    only (see webapp/services/spare_part_movement_service.py) — every
    display path independently derives current stock from the movement
    ledger, so the two can always be reconciled/tested for equality.
    """
    __tablename__ = "spare_parts"

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(20), unique=True, nullable=True, index=True)
    name = db.Column(db.String(200), nullable=False, index=True)
    model = db.Column(db.String(120), nullable=True)
    size = db.Column(db.String(120), nullable=True)
    unit = db.Column(db.String(20), nullable=False, default="pcs")
    minimum_stock = db.Column(db.Numeric(12, 3), nullable=True)
    location = db.Column(db.String(120), nullable=True)
    notes = db.Column(db.Text, nullable=True)
    active = db.Column(db.Boolean, nullable=False, default=True, index=True)
    current_stock_cache = db.Column(db.Numeric(12, 3), nullable=False, default=0)
    created_at = db.Column(db.DateTime(), nullable=False, default=_utcnow)
    updated_at = db.Column(db.DateTime(), nullable=False, default=_utcnow, onupdate=_utcnow)

    def to_dict(self, current_stock=None):
        return {
            "id": self.id, "code": self.code, "name": self.name, "model": self.model,
            "size": self.size, "unit": self.unit,
            "minimum_stock": str(self.minimum_stock) if self.minimum_stock is not None else None,
            "location": self.location, "notes": self.notes, "active": self.active,
            "current_stock": str(current_stock) if current_stock is not None else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class SparePartMachine(db.Model):
    __tablename__ = "spare_part_machines"

    id = db.Column(db.Integer, primary_key=True)
    spare_part_id = db.Column(db.Integer, db.ForeignKey("spare_parts.id"), nullable=False, index=True)
    machine_id = db.Column(db.Integer, db.ForeignKey("machines.id"), nullable=False, index=True)

    __table_args__ = (db.UniqueConstraint("spare_part_id", "machine_id", name="uq_spare_part_machine"),)


class SparePartSupplier(db.Model):
    """
    One sourcing quote for a spare part from a supplier. raw_price_text is
    always the exact original text; normalized_price is only populated
    when a value can be parsed unambiguously (see
    spare_part_import_service.parse_price()) — never guessed.
    """
    __tablename__ = "spare_part_suppliers"

    id = db.Column(db.Integer, primary_key=True)
    spare_part_id = db.Column(db.Integer, db.ForeignKey("spare_parts.id"), nullable=False, index=True)
    supplier_id = db.Column(db.Integer, db.ForeignKey("spare_suppliers.id"), nullable=False, index=True)
    raw_price_text = db.Column(db.String(120), nullable=True)
    normalized_price = db.Column(db.Numeric(12, 2), nullable=True)
    notes = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime(), nullable=False, default=_utcnow)

    __table_args__ = (
        db.UniqueConstraint("spare_part_id", "supplier_id", "raw_price_text", name="uq_spare_part_supplier_quote"),
    )

    def to_dict(self):
        return {
            "id": self.id, "spare_part_id": self.spare_part_id, "supplier_id": self.supplier_id,
            "raw_price_text": self.raw_price_text,
            "normalized_price": str(self.normalized_price) if self.normalized_price is not None else None,
            "notes": self.notes,
        }
