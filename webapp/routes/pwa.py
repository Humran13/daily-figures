"""
Dynamic PWA manifest — takes over "/manifest.webmanifest" from Flask's
static-file serving (Werkzeug always prefers an explicit route over the
catch-all static rule, the same mechanism webapp/routes/pages.py already
relies on for its page guards), so the installed app's icon can reflect
the current company logo without rewriting any file inside the Docker
image at runtime. Falls back to the generic ledger icon shipped as a
static asset whenever no logo-derived icons are configured — see
webapp/services/branding_service.py's manifest_icons().

Deliberately unauthenticated: the browser fetches this with no session at
all (it's linked from every page's <head>, including the login page).
"""
from flask import Blueprint, Response, jsonify

from webapp.services import branding_service as svc

pwa_bp = Blueprint("pwa", __name__)


@pwa_bp.route("/manifest.webmanifest")
def manifest():
    settings = svc.get_settings()
    name = settings.display_name or "Daily Figures"
    body = {
        "name": name,
        "short_name": name[:30],
        "description": "Dispatch, Returns, Production, and Daily Figures stock tracking",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "theme_color": "#1B2430",
        "background_color": "#FCFAF6",
        "icons": svc.manifest_icons(settings),
    }
    resp = jsonify(body)
    resp.mimetype = "application/manifest+json"
    return resp
