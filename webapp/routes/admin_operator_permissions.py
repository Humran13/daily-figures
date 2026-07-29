"""
Role-wide Operator Daily-Figures permission flags. Reading the current
flags is harmless (no secrets, just four booleans) so any authenticated
user may GET them — the Daily Figures page itself needs to know the
current state to render read-only correctly for an Operator. Changing
them is Super Administrator only.
"""
from flask import Blueprint, jsonify, request

from webapp.auth import current_user, login_required, roles_required
from webapp.extensions import db
from webapp.models.user import ROLE_SUPER_ADMIN
from webapp.services import operator_permissions_service as svc

admin_operator_permissions_bp = Blueprint(
    "admin_operator_permissions", __name__, url_prefix="/api/admin/operator-daily-figure-permissions"
)


@admin_operator_permissions_bp.route("", methods=["GET"])
@login_required
def get_permissions():
    return jsonify(svc.get_permissions().to_dict())


@admin_operator_permissions_bp.route("", methods=["PATCH"])
@roles_required(ROLE_SUPER_ADMIN)
def update_permissions():
    d = request.get_json(force=True) or {}
    changes = {k: v for k, v in d.items() if k in svc.FIELDS}
    try:
        permissions = svc.update_permissions(changes, current_user())
    except svc.OperatorPermissionsError as e:
        return jsonify({"error": str(e)}), 400
    db.session.commit()
    return jsonify(permissions.to_dict())
