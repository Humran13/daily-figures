from datetime import datetime, timezone

from webapp.extensions import db


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class SparePartDepartment(db.Model):
    """
    An OPERATIONAL (factory) department that receives spare parts —
    e.g. "Weaving", "Maintenance". Deliberately a separate model/table
    from webapp.models.section.Department (the "Store Department"
    application-SECTION concept used for login/access control). The two
    are unrelated: this one only exists to attribute a Stock Out to the
    department that actually received the spare, and to optionally group
    Machines under it.
    """
    __tablename__ = "spare_part_departments"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), unique=True, nullable=False, index=True)
    active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime(), nullable=False, default=_utcnow)
    updated_at = db.Column(db.DateTime(), nullable=False, default=_utcnow, onupdate=_utcnow)

    def to_dict(self):
        return {"id": self.id, "name": self.name, "active": self.active}
