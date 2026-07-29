"""
Server-side enforcement for the two management-only pages. Flask's static
handler would otherwise serve dashboard.html/admin.html to anyone,
authenticated or not, leaving the existing client-side "gate" divs as the
only thing standing between an unauthorized role and the page content —
that's not real enforcement, just cosmetic. These two explicit routes take
precedence over the generic static rule (Werkzeug always prefers a literal
path over the catch-all static `/<path:filename>` rule) and check the role
before ever returning the file.

Every underlying API these pages call is independently role-gated too
(admin_users_bp, admin_recipient_import_bp, dashboard_bp, reports_bp, ...)
— this page guard is defense in depth for direct URL access, not the only
line of defense.
"""
from flask import Blueprint, current_app, redirect

from webapp.auth import current_user
from webapp.models.user import ROLE_MANAGER, ROLE_SUPER_ADMIN

pages_bp = Blueprint("pages", __name__)

# Where to send a signed-in user who isn't allowed on the page they asked
# for — their own first authorized screen, never a bare error page.
FIRST_AUTHORIZED_PAGE = {
    ROLE_SUPER_ADMIN: "/dashboard.html",
    ROLE_MANAGER: "/dashboard.html",
    "operator": "/dispatch.html?tab=new",
    "viewer": "/dispatch.html?tab=new",
}


def first_authorized_page(user):
    if user is None:
        return "/"
    return FIRST_AUTHORIZED_PAGE.get(user.role, "/")


def _guard_page(filename, allowed_roles):
    user = current_user()
    if user is None:
        return redirect("/")
    if user.role not in allowed_roles:
        return redirect(first_authorized_page(user))
    return current_app.send_static_file(filename)


@pages_bp.route("/dashboard.html")
def dashboard_page():
    return _guard_page("dashboard.html", (ROLE_SUPER_ADMIN, ROLE_MANAGER))


@pages_bp.route("/admin.html")
def admin_page():
    return _guard_page("admin.html", (ROLE_SUPER_ADMIN,))


@pages_bp.route("/history.html")
def history_page():
    # Every role may use History & Exports (Stage 2) — this only enforces
    # that a session exists at all, unlike the role-restricted guards above.
    # Previously served by the generic static handler with no server-side
    # check whatsoever, same gap dashboard.html/admin.html had before Stage 1.
    user = current_user()
    if user is None:
        return redirect("/")
    return current_app.send_static_file("history.html")
