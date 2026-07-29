"""
Role-wide Operator Daily-Figures permission flags. Single row, id=1,
created lazily with all-False defaults if the seed migration hasn't run
yet (e.g. a test database created via db.create_all() rather than
alembic). Manager and Super Admin are never gated by this — only Operator
write attempts on Daily Figures consult it.
"""
from webapp.extensions import db
from webapp.models.operator_daily_figure_permissions import OperatorDailyFigurePermissions
from webapp.services.audit_service import record_audit

FIELDS = ("can_edit_opening", "can_edit_production", "can_edit_returns", "can_create_adjustments")


class OperatorPermissionsError(ValueError):
    pass


def get_permissions():
    permissions = db.session.get(OperatorDailyFigurePermissions, 1)
    if permissions is None:
        permissions = OperatorDailyFigurePermissions(
            id=1, can_edit_opening=False, can_edit_production=False,
            can_edit_returns=False, can_create_adjustments=False,
        )
        db.session.add(permissions)
        db.session.flush()
    return permissions


def update_permissions(changes, user):
    """
    changes: dict of a subset of FIELDS -> bool. Rejects unknown keys and
    non-bool values rather than silently ignoring a typo'd field name.
    """
    unknown = set(changes) - set(FIELDS)
    if unknown:
        raise OperatorPermissionsError(f"unknown permission field(s): {sorted(unknown)}")
    for key, value in changes.items():
        if not isinstance(value, bool):
            raise OperatorPermissionsError(f"{key} must be a boolean")

    permissions = get_permissions()
    before = permissions.to_dict()
    for key, value in changes.items():
        setattr(permissions, key, value)
    permissions.updated_by = user.id
    db.session.flush()
    after = permissions.to_dict()

    record_audit(user, "update", "operator_daily_figure_permissions", entity_id=1, before=before, after=after)
    return permissions
