"""
Server-side enforcement for pages that need more than "you must be logged
in" — either a role restriction, a feature-flag restriction, or both.
Flask's static handler would otherwise serve these files to anyone,
authenticated or not and regardless of flags, leaving the client-side
"gate" divs as the only thing standing in the way — that's not real
enforcement, just cosmetic. These explicit routes take precedence over the
generic static rule (Werkzeug always prefers a literal path over the
catch-all static `/<path:filename>` rule, and Flask lets an explicit
`/` route override its own built-in static index serving the same way).

Every underlying API these pages call is independently guarded too
(role decorators plus feature_required — see webapp/auth.py) — this page
guard is defense in depth for direct URL access, not the only line of
defense.

admin.html is deliberately never feature-flag-gated: it's where a Super
Administrator re-enables a disabled module, so it must always be
reachable regardless of any flag's state.
"""
from flask import Blueprint, Response, current_app, redirect

from webapp.auth import current_user
from webapp.models.user import ROLE_MANAGER, ROLE_SUPER_ADMIN
from webapp.services import feature_flag_service as ffs

pages_bp = Blueprint("pages", __name__)

# Where to send a signed-in user who isn't allowed on the page they asked
# for — their own first authorized screen, never a bare error page.
FIRST_AUTHORIZED_PAGE = {
    ROLE_SUPER_ADMIN: "/dashboard.html",
    ROLE_MANAGER: "/dashboard.html",
    "operator": "/dispatch.html?tab=new",
    "viewer": "/dispatch.html?tab=new",
}

# Ordered fallback chain for "the module this page needs is off — where
# else could this user reasonably go?"
MODULE_PAGES = [
    ("daily_figures", "/"), ("dispatch", "/dispatch.html"),
    ("returns", "/returns.html"), ("production", "/production.html"),
    ("history_exports", "/history.html"),
]

_DISABLED_MODULE_MESSAGE = "This module is currently disabled. Contact your administrator."


def first_authorized_page(user):
    if user is None:
        return "/"
    return FIRST_AUTHORIZED_PAGE.get(user.role, "/")


def _fallback_for(exclude_module):
    for module_key, path in MODULE_PAGES:
        if module_key != exclude_module and ffs.is_enabled(module_key):
            return path
    return None


def _module_disabled_response(module_key):
    fallback = _fallback_for(module_key)
    if fallback:
        return redirect(fallback)
    return Response(_DISABLED_MODULE_MESSAGE, status=503)


def _guard_page(filename, allowed_roles, module_key=None):
    user = current_user()
    if user is None:
        return redirect("/")
    if user.role not in allowed_roles:
        return redirect(first_authorized_page(user))
    if module_key and not ffs.is_enabled(module_key):
        return _module_disabled_response(module_key)
    return current_app.send_static_file(filename)


def _guard_flag_only(filename, module_key):
    # "/dispatch.html" has never required a session at the page level (it
    # shows its own client-side "sign in" gate) — only the feature flag is
    # enforced at this layer; auth is enforced by every API this page calls.
    if not ffs.is_enabled(module_key):
        return _module_disabled_response(module_key)
    return current_app.send_static_file(filename)


def _guard_flag_and_auth(filename, module_key):
    user = current_user()
    if user is None:
        return redirect("/")
    if not ffs.is_enabled(module_key):
        return _module_disabled_response(module_key)
    return current_app.send_static_file(filename)


# "/" (index.html) is deliberately NEVER guarded here, by either auth or
# feature flag: it's the app's only login page. Redirecting an
# unauthenticated visitor away from it would be a loop back to itself, and
# gating it on the "daily_figures" flag would lock out every login the
# moment that module is disabled, taking every other page down with it
# (they all redirect an unauthenticated visitor to "/"). Daily Figures
# being disabled is instead enforced client-side, after a successful
# login, by index.html's own boot logic (mirrors the existing role-based
# post-login redirect) plus the already-guarded /api/daily-figures/* routes.

@pages_bp.route("/dispatch.html")
def dispatch_page():
    return _guard_flag_only("dispatch.html", "dispatch")


@pages_bp.route("/returns.html")
def returns_page():
    return _guard_flag_only("returns.html", "returns")


@pages_bp.route("/production.html")
def production_page():
    return _guard_flag_only("production.html", "production")


@pages_bp.route("/dashboard.html")
def dashboard_page():
    return _guard_page("dashboard.html", (ROLE_SUPER_ADMIN, ROLE_MANAGER), module_key="dashboard")


@pages_bp.route("/admin.html")
def admin_page():
    return _guard_page("admin.html", (ROLE_SUPER_ADMIN,))


@pages_bp.route("/history.html")
def history_page():
    return _guard_flag_and_auth("history.html", "history_exports")
