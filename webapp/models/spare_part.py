from datetime import datetime, timezone

from webapp.extensions import db

UNITS = ["pcs", "rolls", "metres", "sets", "litres", "other"]
COST_SOURCE_CHINA = "china"
COST_SOURCE_LOCAL = "local_uganda"
COST_SOURCES = (COST_SOURCE_CHINA, COST_SOURCE_LOCAL)


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def profit_and_margin(buying, selling):
    """
    The single profit/margin convention for the whole app: Profit = Selling
    - Buying; Margin % = Profit / Selling * 100. Returns (None, None) when
    either price is missing, and a None margin when selling is zero — never
    fabricates a result from insufficient data.
    """
    if buying is None or selling is None:
        return None, None
    profit = selling - buying
    margin = (profit / selling) * 100 if selling != 0 else None
    return profit, margin


def current_cost(spare_part):
    """Return the explicitly selected source cost.

    ``buying_price`` is retained as a read-compatible legacy fallback for
    records migrated from the former single-price model.  It is deliberately
    not copied into either source column because its origin is unknowable.
    """
    if spare_part.preferred_cost_source == COST_SOURCE_CHINA:
        return spare_part.china_buying_price
    if spare_part.preferred_cost_source == COST_SOURCE_LOCAL:
        return spare_part.local_buying_price
    return spare_part.buying_price


def pricing_dict(spare_part):
    """
    String-serialized pricing for API responses. Callers MUST gate this
    behind a Super Admin check before including it in any response (see
    webapp/routes/spare_parts.py's _may_see_pricing()).
    """
    buying = current_cost(spare_part)
    selling = spare_part.selling_price
    profit, margin = profit_and_margin(buying, selling)
    return {
        "buying_price": str(buying) if buying is not None else None,
        "china_buying_price": str(spare_part.china_buying_price) if spare_part.china_buying_price is not None else None,
        "local_buying_price": str(spare_part.local_buying_price) if spare_part.local_buying_price is not None else None,
        "preferred_cost_source": spare_part.preferred_cost_source,
        "current_cost_price": str(buying) if buying is not None else None,
        "selling_price": str(selling) if selling is not None else None,
        "profit": str(profit) if profit is not None else None,
        "margin_percent": str(margin) if margin is not None else None,
    }


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
    # model/size are legacy/import-provenance fields — still stored for
    # traceability, but the normal UI reads/writes `specifications`
    # instead (see webapp/services/spare_part_service.py's
    # combine_specifications()). Never dropped, never required again.
    model = db.Column(db.String(120), nullable=True)
    size = db.Column(db.String(120), nullable=True)
    specifications = db.Column(db.String(255), nullable=True)
    category_id = db.Column(db.Integer, db.ForeignKey("spare_part_categories.id"), nullable=True)
    unit = db.Column(db.String(20), nullable=False, default="pcs")
    minimum_stock = db.Column(db.Numeric(12, 3), nullable=True)
    location = db.Column(db.String(120), nullable=True)
    notes = db.Column(db.Text, nullable=True)
    # Commercial fields — restricted to Super Admin at the route
    # layer (webapp/routes/spare_parts.py's _to_dict()); never float.
    buying_price = db.Column(db.Numeric(12, 2), nullable=True)
    china_buying_price = db.Column(db.Numeric(12, 2), nullable=True)
    local_buying_price = db.Column(db.Numeric(12, 2), nullable=True)
    preferred_cost_source = db.Column(db.String(20), nullable=True)
    selling_price = db.Column(db.Numeric(12, 2), nullable=True)
    active = db.Column(db.Boolean, nullable=False, default=True, index=True)
    current_stock_cache = db.Column(db.Numeric(12, 3), nullable=False, default=0)
    created_at = db.Column(db.DateTime(), nullable=False, default=_utcnow)
    updated_at = db.Column(db.DateTime(), nullable=False, default=_utcnow, onupdate=_utcnow)

    def to_dict(self, current_stock=None, include_pricing=False):
        d = {
            "id": self.id, "code": self.code, "name": self.name, "model": self.model,
            "size": self.size, "specifications": self.specifications, "category_id": self.category_id,
            "unit": self.unit,
            "minimum_stock": str(self.minimum_stock) if self.minimum_stock is not None else None,
            "location": self.location, "notes": self.notes, "active": self.active,
            "current_stock": str(current_stock) if current_stock is not None else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
        if include_pricing:
            d.update(pricing_dict(self))
        return d


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
