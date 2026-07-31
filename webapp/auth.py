"""
Individual accounts + role-based access, replacing the old shared PIN.
Session-based (same mechanism the old app used), just keyed by user id
instead of a boolean, with a role loaded alongside it.
"""
import functools
import logging
import os
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request, session
from werkzeug.security import check_password_hash, generate_password_hash

from webapp.extensions import db
from webapp.models.user import ROLES, User
from webapp.services.audit_service import record_audit

logger = logging.getLogger(__name__)

auth_bp = Blueprint("auth", __name__, url_prefix="/api")


SESSION_SUPERSEDED_MESSAGE = "Your account was signed in on another device."


def _session_diagnosis():
    """
    Distinguishes *why* the current request isn't authenticated as a valid
    user, so callers that want to can surface
    SESSION_SUPERSEDED_MESSAGE specifically rather than a generic
    "unauthorized" — reuses the exact same session_version mechanism
    password-reset invalidation already relies on (see User.session_version's
    docstring), just bumped on every successful login now too (see login()
    below) rather than only on a password reset. One active session per
    user falls out of that for free: a second login bumps the counter, so
    the first session's stamped value no longer matches and every one of
    its subsequent requests is treated as logged-out.

    Returns ("ok", user) | ("unauthenticated", None) | ("superseded", None).
    """
    user_id = session.get("user_id")
    if user_id is None:
        return "unauthenticated", None
    user = db.session.get(User, user_id)
    if user is None or not user.active:
        return "unauthenticated", None
    # Missing from the session entirely (e.g. a cookie issued before this
    # check existed) defaults to 0, matching a fresh user's column default,
    # so this never forces every existing session to re-authenticate —
    # only ones whose session_version has since moved on without them.
    if session.get("session_version", 0) != (user.session_version or 0):
        return "superseded", None
    return "ok", user


def current_user():
    status, user = _session_diagnosis()
    return user if status == "ok" else None


def _unauthorized_response():
    status, _ = _session_diagnosis()
    if status == "superseded":
        return jsonify({"error": SESSION_SUPERSEDED_MESSAGE, "session_superseded": True}), 401
    return jsonify({"error": "unauthorized"}), 401


def login_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if current_user() is None:
            return _unauthorized_response()
        return view(*args, **kwargs)
    return wrapped


def roles_required(*allowed_roles):
    def decorator(view):
        @functools.wraps(view)
        def wrapped(*args, **kwargs):
            user = current_user()
            if user is None:
                return _unauthorized_response()
            if user.role not in allowed_roles:
                return jsonify({"error": "forbidden"}), 403
            return view(*args, **kwargs)
        return wrapped
    return decorator


def feature_required(module_key):
    """
    Blocks a route when its module has been disabled via Company Settings
    > Feature Flags (Super Administrator only — see
    webapp/services/feature_flag_service.py). Independent of and stacked
    with login_required/roles_required: a feature being off is not a
    permission question, so this never returns 401 — just a 403 naming the
    disabled module. Disabling a module never touches its data; this only
    blocks the route while the flag is off.
    """
    def decorator(view):
        @functools.wraps(view)
        def wrapped(*args, **kwargs):
            from webapp.services import feature_flag_service
            if not feature_flag_service.is_enabled(module_key):
                return jsonify({"error": f"the '{module_key}' module is currently disabled"}), 403
            return view(*args, **kwargs)
        return wrapped
    return decorator


@auth_bp.route("/login", methods=["POST"])
def login():
    data = request.get_json(force=True) or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""

    user = User.query.filter_by(username=username).first()
    if user is None or not user.active or not check_password_hash(user.password_hash, password):
        record_audit(None, "login_failed", "user", entity_id=username or None)
        db.session.commit()
        return jsonify({"ok": False, "error": "Invalid username or password"}), 401

    # One active session per account (Stage 7 section 2): every successful
    # login bumps session_version, so whatever session was previously
    # stamped with the old value — on this device or any other — stops
    # authenticating on its very next request (see _session_diagnosis()
    # above). A failed login attempt never reaches this line, so it can
    # never invalidate the real, currently-valid session.
    user.session_version = (user.session_version or 0) + 1
    session.clear()
    session["user_id"] = user.id
    session["session_version"] = user.session_version
    session.permanent = True
    user.last_login_at = datetime.now(timezone.utc).replace(tzinfo=None)
    record_audit(user, "login_success", "user", entity_id=user.id)
    db.session.commit()
    return jsonify({"ok": True, "user": user.to_dict()})


@auth_bp.route("/session", methods=["GET"])
def check_session():
    status, user = _session_diagnosis()
    body = {"authed": status == "ok", "user": user.to_dict() if user else None}
    if status == "superseded":
        body["session_superseded"] = True
        body["message"] = SESSION_SUPERSEDED_MESSAGE
    return jsonify(body)


@auth_bp.route("/logout", methods=["POST"])
def logout():
    user = current_user()
    if user is not None:
        record_audit(user, "logout", "user", entity_id=user.id)
        db.session.commit()
    session.clear()
    return jsonify({"ok": True})


def seed_super_admin():
    """
    Create the first super-admin from environment variables if no user
    exists yet. Never invents a default password — if the env vars are
    missing, the app comes up with zero users and logs a loud warning
    instead of a hardcoded credential.
    """
    if User.query.count() > 0:
        return

    username = os.environ.get("SUPERADMIN_USERNAME")
    password = os.environ.get("SUPERADMIN_PASSWORD")
    if not username or not password:
        logger.warning(
            "No users exist and SUPERADMIN_USERNAME/SUPERADMIN_PASSWORD are not set. "
            "Set both environment variables and restart to create the first account."
        )
        return

    from webapp.models.user import ROLE_SUPER_ADMIN
    admin = User(
        username=username,
        password_hash=generate_password_hash(password),
        role=ROLE_SUPER_ADMIN,
        active=True,
    )
    db.session.add(admin)
    record_audit(None, "seed_super_admin", "user", entity_id=username)
    db.session.commit()
    logger.info("Created initial super-admin account %r", username)
