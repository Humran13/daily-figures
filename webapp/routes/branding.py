"""
Public branding endpoints — deliberately unauthenticated, since the login
page (and every other page's header, before any API call has confirmed a
session) needs the company name/logo to render. Only ever returns the
safe display subset (CompanySettings.public_dict()); every other field
lives behind /api/admin/company-settings, which is super_admin only.
"""
import os

from flask import Blueprint, abort, jsonify, send_file

from webapp.services import branding_service as svc

branding_bp = Blueprint("branding", __name__, url_prefix="/api/branding")


@branding_bp.route("", methods=["GET"])
def get_public_branding():
    return jsonify(svc.get_settings().public_dict())


@branding_bp.route("/logo", methods=["GET"])
def get_logo():
    path = svc.logo_file_path()
    if path is None or not os.path.exists(path):
        abort(404)
    return send_file(path)
