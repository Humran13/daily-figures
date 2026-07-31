"""
Public branding endpoints — deliberately unauthenticated, since the login
page (and every other page's header, before any API call has confirmed a
session) needs the company name/logo to render. Only ever returns the
safe display subset (CompanySettings.public_dict()); every other field
lives behind /api/admin/company-settings, which is super_admin only.

The three icon-*.png routes back the PWA manifest (see webapp/routes/pwa.py
and webapp/services/branding_service.py's manifest_icons()) — also
unauthenticated for the same reason: a browser fetches manifest icons with
no session at all, including on the login page.
"""
import os

from flask import Blueprint, abort, current_app, jsonify, send_file

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


def _serve_icon_or_fallback(size, maskable, fallback_filename):
    path = svc.icon_file_path(size, maskable=maskable)
    if path and os.path.exists(path):
        return send_file(path)
    return current_app.send_static_file(f"icons/{fallback_filename}")


@branding_bp.route("/icon-192.png", methods=["GET"])
def get_icon_192():
    return _serve_icon_or_fallback(192, False, "icon-192.png")


@branding_bp.route("/icon-512.png", methods=["GET"])
def get_icon_512():
    return _serve_icon_or_fallback(512, False, "icon-512.png")


@branding_bp.route("/icon-512-maskable.png", methods=["GET"])
def get_icon_512_maskable():
    return _serve_icon_or_fallback(512, True, "icon-maskable-512.png")
