from flask import Blueprint, jsonify, request

from webapp.auth import current_user, roles_required, feature_required
from webapp.extensions import db
from webapp.models.user import ROLE_SUPER_ADMIN
from webapp.services import daily_reset_service as svc

daily_reset_bp = Blueprint("daily_reset", __name__, url_prefix="/api/daily-reset")


def _error(e, status=400):
    return jsonify({"error": str(e)}), status


def _product_id_from(d):
    raw = d.get("product_id")
    return int(raw) if raw not in (None, "", "all") else None


@daily_reset_bp.route("/preview", methods=["POST"])
@roles_required(ROLE_SUPER_ADMIN)
@feature_required("daily_figures")
def preview():
    d = request.get_json(force=True) or {}
    try:
        result = svc.preview(d.get("date"), d.get("shift"), _product_id_from(d), current_user())
    except svc.DailyResetError as e:
        return _error(e)
    return jsonify(result)


@daily_reset_bp.route("", methods=["POST"])
@roles_required(ROLE_SUPER_ADMIN)
@feature_required("daily_figures")
def execute():
    d = request.get_json(force=True) or {}
    actor = current_user()
    try:
        result = svc.execute(d.get("date"), d.get("shift"), _product_id_from(d), d.get("reason"), actor)
    except svc.DailyResetError as e:
        db.session.rollback()
        return _error(e)
    db.session.commit()
    return jsonify(result)
