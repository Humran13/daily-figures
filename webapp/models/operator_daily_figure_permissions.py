from datetime import datetime, timezone

from webapp.extensions import db


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class OperatorDailyFigurePermissions(db.Model):
    """
    Single-row, role-wide switch for whether the Operator role may edit
    Daily Figures fields directly. Dispatch remains the source of truth
    for Issued regardless of these flags — there is no "can_edit_issued"
    because Issued has no writable column anywhere in the schema.

    All four flags default to False (Operator is read-only on Daily
    Figures until a Super Administrator explicitly turns one on). Manager
    and Super Admin are never affected by this row — their existing
    unconditional write access is untouched. Viewer is never affected
    either — Viewer's read-only status does not come from here and these
    flags must never be read as granting Viewer anything.
    """
    __tablename__ = "operator_daily_figure_permissions"

    id = db.Column(db.Integer, primary_key=True)
    can_edit_opening = db.Column(db.Boolean, nullable=False, default=False)
    can_edit_production = db.Column(db.Boolean, nullable=False, default=False)
    can_edit_returns = db.Column(db.Boolean, nullable=False, default=False)
    can_create_adjustments = db.Column(db.Boolean, nullable=False, default=False)
    updated_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    updated_at = db.Column(db.DateTime(), nullable=False, default=_utcnow, onupdate=_utcnow)

    def to_dict(self):
        return {
            "can_edit_opening": self.can_edit_opening,
            "can_edit_production": self.can_edit_production,
            "can_edit_returns": self.can_edit_returns,
            "can_create_adjustments": self.can_create_adjustments,
            "updated_by": self.updated_by,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
