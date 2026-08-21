from flask import Blueprint, jsonify, request

from webapp.auth import roles_required, feature_required
from webapp.models.user import ROLE_ACCOUNTANT, ROLE_MANAGER, ROLE_SUPER_ADMIN, ROLE_VIEWER
from webapp.services.business_calendar import business_today
from webapp.services.dashboard_service import build_dashboard

dashboard_bp = Blueprint("dashboard", __name__, url_prefix="/api/dashboard")


@dashboard_bp.route("", methods=["GET"])
@roles_required(ROLE_SUPER_ADMIN, ROLE_MANAGER, ROLE_ACCOUNTANT, ROLE_VIEWER)
@feature_required("dashboard")
def get_dashboard():
    # Africa/Kampala "today", not server UTC — near Kampala midnight
    # (e.g. 22:30 UTC == 01:30 EAT the next day) the two disagree by a
    # full calendar day, which would show the wrong business date's
    # dashboard by default whenever a caller omits ?date=.
    today = request.args.get("date") or business_today()
    return jsonify(build_dashboard(today))
