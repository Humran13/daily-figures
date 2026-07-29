"""
Full Company Settings management — super_admin only, per Stage 3's
explicit requirement (Managers, Operators, and Viewers must not modify
branding; reading the full admin form is restricted the same way since it
only ever renders inside the super_admin-gated Admin page).
"""
from flask import Blueprint, jsonify, request

from webapp.auth import current_user, roles_required
from webapp.extensions import db
from webapp.models.user import ROLE_SUPER_ADMIN
from webapp.services import branding_service as svc

admin_company_settings_bp = Blueprint(
    "admin_company_settings", __name__, url_prefix="/api/admin/company-settings"
)


@admin_company_settings_bp.route("", methods=["GET"])
@roles_required(ROLE_SUPER_ADMIN)
def get_company_settings():
    return jsonify(svc.get_settings().to_dict())


@admin_company_settings_bp.route("", methods=["PATCH"])
@roles_required(ROLE_SUPER_ADMIN)
def update_company_settings():
    d = request.get_json(force=True) or {}
    try:
        settings = svc.update_settings(d, current_user())
    except svc.BrandingError as e:
        return jsonify({"error": str(e)}), 400
    db.session.commit()
    return jsonify(settings.to_dict())


@admin_company_settings_bp.route("/logo", methods=["POST"])
@roles_required(ROLE_SUPER_ADMIN)
def upload_logo():
    try:
        settings = svc.upload_logo(request.files.get("logo"), current_user())
    except svc.BrandingError as e:
        return jsonify({"error": str(e)}), 400
    db.session.commit()
    return jsonify(settings.to_dict())


@admin_company_settings_bp.route("/logo", methods=["DELETE"])
@roles_required(ROLE_SUPER_ADMIN)
def remove_logo():
    try:
        settings = svc.remove_logo(current_user())
    except svc.BrandingError as e:
        return jsonify({"error": str(e)}), 400
    db.session.commit()
    return jsonify(settings.to_dict())
