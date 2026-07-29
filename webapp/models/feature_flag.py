from datetime import datetime, timezone

from webapp.extensions import db

# The modules this app actually has as distinct sections today. As of
# Stage 5, Returns and Production are their own dedicated books/modules
# (Dispatch/Returns/Production all feed Daily Figures) rather than fields
# entered directly on Daily Figures — see webapp/models/return_record.py,
# webapp/models/production_record.py.
MODULE_DISPATCH = "dispatch"
MODULE_DAILY_FIGURES = "daily_figures"
MODULE_HISTORY_EXPORTS = "history_exports"
MODULE_DASHBOARD = "dashboard"
MODULE_CUSTOMER_MANAGEMENT = "customer_management"
MODULE_REPORTING = "reporting"
MODULE_RETURNS = "returns"
MODULE_PRODUCTION = "production"

MODULES = [
    MODULE_DISPATCH,
    MODULE_DAILY_FIGURES,
    MODULE_HISTORY_EXPORTS,
    MODULE_DASHBOARD,
    MODULE_CUSTOMER_MANAGEMENT,
    MODULE_REPORTING,
    MODULE_RETURNS,
    MODULE_PRODUCTION,
]

MODULE_LABELS = {
    MODULE_DISPATCH: "Dispatch",
    MODULE_DAILY_FIGURES: "Daily Figures",
    MODULE_HISTORY_EXPORTS: "History & Exports",
    MODULE_DASHBOARD: "Dashboard",
    MODULE_CUSTOMER_MANAGEMENT: "Customer Management",
    MODULE_REPORTING: "Reporting",
    MODULE_RETURNS: "Returns",
    MODULE_PRODUCTION: "Production",
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
