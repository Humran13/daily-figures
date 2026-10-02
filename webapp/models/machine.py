from datetime import datetime, timezone

from webapp.extensions import db


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Machine(db.Model):
    """
    Canonical machine master. A Machine is reached only through
    MachineAlias — the Spare Parts importer never writes a raw workbook
    string straight into Machine.name, so re-pointing a messy alias later
    (via Admin) never loses the original workbook text (see MachineAlias).
    """
    __tablename__ = "machines"

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(60), unique=True, nullable=False, index=True)
    name = db.Column(db.String(120), nullable=False)
    active = db.Column(db.Boolean, nullable=False, default=True)
    notes = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime(), nullable=False, default=_utcnow)
    updated_at = db.Column(db.DateTime(), nullable=False, default=_utcnow, onupdate=_utcnow)

    def to_dict(self):
        return {
            "id": self.id, "code": self.code, "name": self.name, "active": self.active,
            "notes": self.notes,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class MachineAlias(db.Model):
    """
    One row per distinct raw spelling ever seen for a machine (from the
    workbook import or future manual entry), pointing at its canonical
    Machine. normalized_text (trimmed + casefolded) is the actual
    identity key the importer dedupes on — raw_text is preserved verbatim
    for traceability and so Admin can re-point an alias later without
    losing the original source text.
    """
    __tablename__ = "machine_aliases"

    id = db.Column(db.Integer, primary_key=True)
    machine_id = db.Column(db.Integer, db.ForeignKey("machines.id"), nullable=False)
    raw_text = db.Column(db.String(120), nullable=False)
    normalized_text = db.Column(db.String(120), unique=True, nullable=False, index=True)
    created_at = db.Column(db.DateTime(), nullable=False, default=_utcnow)

    def to_dict(self):
        return {
            "id": self.id, "machine_id": self.machine_id,
            "raw_text": self.raw_text, "normalized_text": self.normalized_text,
        }
