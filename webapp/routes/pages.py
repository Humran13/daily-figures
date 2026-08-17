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
from webapp.models.user import ROLE_MANAGER, ROLE_OPERATOR, ROLE_SUPER_ADMIN, ROLE_VIEWER
from webapp.services import feature_flag_service as ffs

pages_bp = Blueprint("pages", __name__)

# Stage 6: Viewer now lands on Dashboard alongside Manager/Super Admin
# (read-only there, same as everywhere else) — only Operator gets a
# different landing page, resolved below via OPERATOR_LANDING_CHAIN
# instead of a fixed path, since which operational book is "first" depends
# on which of Dispatch/Returns/Production is actually enabled.
FIRST_AUTHORIZED_PAGE = {
    ROLE_SUPER_ADMIN: "/dashboard.html",
    ROLE_MANAGER: "/dashboard.html",
    ROLE_VIEWER: "/dashboard.html",
}

# Operator's landing page, and the "Home" destination the shared
# static/app-shell.js mirrors client-side (see its resolveLanding()) —
# the first enabled+authorized operational book, never Dashboard (Operators
# never see Dashboard at all).
OPERATOR_LANDING_CHAIN = [
    ("dispatch", "/dispatch.html?tab=new"),
    ("returns", "/returns.html?tab=new"),
    ("production", "/production.html?tab=new"),
]

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
    if user.role == ROLE_OPERATOR:
        for module_key, path in OPERATOR_LANDING_CHAIN:
            if ffs.is_enabled(module_key):
                return path
        # Every operational book is disabled — still send them somewhere
        # real rather than nowhere; the page itself shows the disabled-
        # module fallback/message once they land there.
        return OPERATOR_LANDING_CHAIN[0][1]
    return FIRST_AUTHORIZED_PAGE.get(user.role, "/dashboard.html")


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
    return _guard_page("dashboard.html", (ROLE_SUPER_ADMIN, ROLE_MANAGER, ROLE_VIEWER), module_key="dashboard")


@pages_bp.route("/admin.html")
def admin_page():
    return _guard_page("admin.html", (ROLE_SUPER_ADMIN,))


@pages_bp.route("/reset-daily-values.html")
def reset_daily_values_page():
    # Full targeted Operator correction/void/requests/notification
    # package: Reset Daily Values is now Super-Administrator ONLY —
    # tightened from the prior Manager-or-Super-Administrator rule (see
    # webapp/routes/daily_reset.py's own routes and static/app-shell.js's
    # nav for the matching UI/API restriction). Gated on the same
    # "daily_figures" module the underlying /api/daily-reset* endpoints
    # already require via @feature_required, so a disabled Daily Figures
    # module hides this page exactly the same way it already hides the
    # reset APIs.
    return _guard_page("reset-daily-values.html", (ROLE_SUPER_ADMIN,), module_key="daily_figures")


@pages_bp.route("/history.html")
def history_page():
    return _guard_flag_and_auth("history.html", "history_exports")


@pages_bp.route("/requests.html")
def requests_page():
    # Full targeted Operator correction/void/requests/notification
    # package — the new top-level Requests destination (correction-
    # request review, moved here from the removed internal tab inside
    # history.html) is Manager-or-Super-Administrator only, exactly like
    # the underlying /api/correction-requests* review endpoints
    # (approve/reject/pending-count).
    return _guard_page("requests.html", (ROLE_SUPER_ADMIN, ROLE_MANAGER))
