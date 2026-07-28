from datetime import datetime, timezone

from flask import Blueprint, jsonify, request

from webapp.auth import login_required
from webapp.services.dashboard_service import build_dashboard

dashboard_bp = Blueprint("dashboard", __name__, url_prefix="/api/dashboard")


@dashboard_bp.route("", methods=["GET"])
@login_required
def get_dashboard():
    today = request.args.get("date") or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return jsonify(build_dashboard(today))
