"""
Feature-flag read/write. Reading is login_required (any authenticated
role) since every page needs to know which modules are enabled to decide
its own navigation — same posture as the operator Daily-Figures
permission flags. Only a Super Administrator may change a flag.

PATCH returns {"changed": [...]} — a list, even for a single-flag change
— because disabling a module that other enabled modules depend on can
atomically disable more than one flag at once (cascade: true). See
webapp/services/feature_flag_service.py for the dependency rules.
"""
from flask import Blueprint, jsonify, request

from webapp.auth import current_user, login_required, roles_required
from webapp.extensions import db
from webapp.models.user import ROLE_SUPER_ADMIN
from webapp.services import feature_flag_service as svc

feature_flags_bp = Blueprint("feature_flags", __name__, url_prefix="/api/feature-flags")


@feature_flags_bp.route("", methods=["GET"])
@login_required
def list_flags():
    return jsonify([f.to_dict() for f in svc.get_all_flags()])


@feature_flags_bp.route("/<module_key>", methods=["PATCH"])
@roles_required(ROLE_SUPER_ADMIN)
def update_flag(module_key):
    d = request.get_json(force=True) or {}
    if "enabled" not in d:
        return jsonify({"error": "enabled is required"}), 400
    try:
        changed = svc.set_flag(module_key, bool(d["enabled"]), current_user(), cascade=bool(d.get("cascade")))
    except svc.FeatureFlagError as e:
        # Nothing was flushed before the error in any current code path
        # (every validation happens before the first mutation), but roll
        # back explicitly anyway so a failed dependency check can never
        # leave a partial change sitting in the session.
        db.session.rollback()
        return jsonify({"error": str(e)}), 400
    db.session.commit()
    return jsonify({"changed": [f.to_dict() for f in changed]})
