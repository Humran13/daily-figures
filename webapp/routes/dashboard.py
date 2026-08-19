from datetime import datetime, timezone

from flask import Blueprint, jsonify, request

from webapp.auth import roles_required, feature_required
from webapp.models.user import ROLE_ACCOUNTANT, ROLE_MANAGER, ROLE_SUPER_ADMIN, ROLE_VIEWER
from webapp.services.dashboard_service import build_dashboard

dashboard_bp = Blueprint("dashboard", __name__, url_prefix="/api/dashboard")


@dashboard_bp.route("", methods=["GET"])
@roles_required(ROLE_SUPER_ADMIN, ROLE_MANAGER, ROLE_ACCOUNTANT, ROLE_VIEWER)
@feature_required("dashboard")
def get_dashboard():
    today = request.args.get("date") or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return jsonify(build_dashboard(today))
