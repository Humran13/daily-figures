from datetime import datetime, timezone

from webapp.extensions import db

# The modules this app actually has as distinct sections today. The
# original commercial-reuse plan mentioned "Inventory" and "Production" as
# possible future modules, but neither exists as a distinct section in
# this codebase (Production is a field inside Daily Figures, not a module
# of its own) — flagging something that doesn't exist would just be dead
# UI, so this list only covers real, currently-reachable app sections.
MODULE_DISPATCH = "dispatch"
MODULE_DAILY_FIGURES = "daily_figures"
MODULE_HISTORY_EXPORTS = "history_exports"
MODULE_DASHBOARD = "dashboard"
MODULE_CUSTOMER_MANAGEMENT = "customer_management"
MODULE_REPORTING = "reporting"

MODULES = [
    MODULE_DISPATCH,
    MODULE_DAILY_FIGURES,
    MODULE_HISTORY_EXPORTS,
    MODULE_DASHBOARD,
    MODULE_CUSTOMER_MANAGEMENT,
    MODULE_REPORTING,
]

MODULE_LABELS = {
    MODULE_DISPATCH: "Dispatch",
    MODULE_DAILY_FIGURES: "Daily Figures",
    MODULE_HISTORY_EXPORTS: "History & Exports",
    MODULE_DASHBOARD: "Dashboard",
    MODULE_CUSTOMER_MANAGEMENT: "Customer Management",
    MODULE_REPORTING: "Reporting",
}


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class FeatureFlag(db.Model):
    """
    One row per module. Disabling a flag never deletes or hides existing
    data — it only blocks the module's own routes/nav going forward (see
    webapp/services/feature_flag_service.py and webapp/auth.py's
    feature_required decorator). Re-enabling immediately restores access
    to whatever data was already there.
    """
    __tablename__ = "feature_flags"

    id = db.Column(db.Integer, primary_key=True)
    module_key = db.Column(db.String(40), unique=True, nullable=False, index=True)
    enabled = db.Column(db.Boolean, nullable=False, default=True)
    updated_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    updated_at = db.Column(db.DateTime(), nullable=False, default=_utcnow, onupdate=_utcnow)

    def to_dict(self):
        return {
            "module_key": self.module_key,
            "label": MODULE_LABELS.get(self.module_key, self.module_key),
            "enabled": self.enabled,
            "updated_by": self.updated_by,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
