from datetime import datetime, timezone

from webapp.extensions import db


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class PackagingRule(db.Model):
    """
    A time-bound conversion rule for one product. Rules are never edited in
    place — changing a rule closes the current row (sets effective_to) and
    inserts a new one. Historical dispatch lines snapshot the rule id that
    was active when they were entered, so changing a rule later never
    reinterprets past transactions.
    """

    __tablename__ = "packaging_rules"

    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"), nullable=False, index=True)

    # Group A / Napkins / Straws / Silky 4-pack / Kitchen Towel Doubles style:
    # both set. KingMax / JumboMax / Kitchen Towel Singles style (no pack
    # tier): both null, carton_to_pieces set instead.
    cartons_to_packs = db.Column(db.Integer, nullable=True)
    packs_to_pieces = db.Column(db.Integer, nullable=True)
    carton_to_pieces = db.Column(db.Integer, nullable=True)

    effective_from = db.Column(db.DateTime(), nullable=False, default=_utcnow)
    effective_to = db.Column(db.DateTime(), nullable=True)

    created_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    created_at = db.Column(db.DateTime(), nullable=False, default=_utcnow)

    product = db.relationship("Product", back_populates="packaging_rules")

    __table_args__ = (
        db.CheckConstraint(
            "(cartons_to_packs IS NOT NULL AND packs_to_pieces IS NOT NULL AND carton_to_pieces IS NULL) "
            "OR (cartons_to_packs IS NULL AND packs_to_pieces IS NULL AND carton_to_pieces IS NOT NULL)",
            name="ck_packaging_rule_shape",
        ),
    )

    @property
    def has_pack_tier(self):
        return self.cartons_to_packs is not None

    @property
    def pieces_per_carton(self):
        if self.has_pack_tier:
            return self.cartons_to_packs * self.packs_to_pieces
        return self.carton_to_pieces

    @property
    def pieces_per_pack(self):
        return self.packs_to_pieces if self.has_pack_tier else None

    def to_dict(self):
        return {
            "id": self.id,
            "product_id": self.product_id,
            "cartons_to_packs": self.cartons_to_packs,
            "packs_to_pieces": self.packs_to_pieces,
            "carton_to_pieces": self.carton_to_pieces,
            "effective_from": self.effective_from.isoformat() if self.effective_from else None,
            "effective_to": self.effective_to.isoformat() if self.effective_to else None,
        }
