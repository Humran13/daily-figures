from flask import Blueprint, jsonify, request
from werkzeug.security import generate_password_hash

from webapp.auth import current_user, roles_required
from webapp.extensions import db
from webapp.models.user import ROLES, ROLE_SUPER_ADMIN, User
from webapp.services.audit_service import record_audit

admin_users_bp = Blueprint("admin_users", __name__, url_prefix="/api/admin/users")


def _is_last_active_super_admin(user):
    if user.role != ROLE_SUPER_ADMIN or not user.active:
        return False
    return User.query.filter_by(role=ROLE_SUPER_ADMIN, active=True).count() <= 1


@admin_users_bp.route("", methods=["GET"])
@roles_required(ROLE_SUPER_ADMIN)
def list_users():
    return jsonify([u.to_dict() for u in User.query.order_by(User.username).all()])


@admin_users_bp.route("", methods=["POST"])
@roles_required(ROLE_SUPER_ADMIN)
def create_user():
    d = request.get_json(force=True) or {}
    username = (d.get("username") or "").strip()
    password = d.get("password") or ""
    role = d.get("role")

    if not username or not password:
        return jsonify({"error": "username and password are required"}), 400
    if len(password) < 8:
        return jsonify({"error": "password must be at least 8 characters"}), 400
    if role not in ROLES:
        return jsonify({"error": f"role must be one of {ROLES}"}), 400
    if User.query.filter_by(username=username).first():
        return jsonify({"error": "a user with this username already exists"}), 409

    user = User(username=username, password_hash=generate_password_hash(password), role=role, active=True)
    db.session.add(user)
    db.session.flush()
    record_audit(current_user(), "create", "user", entity_id=user.id, after={"username": username, "role": role})
    db.session.commit()
    return jsonify(user.to_dict()), 201


@admin_users_bp.route("/<int:user_id>", methods=["PATCH"])
@roles_required(ROLE_SUPER_ADMIN)
def update_user(user_id):
    """
    Username/role/active only — password changes are their own endpoint
    (reset_password() below) so audit entries stay cleanly typed ("update"
    vs "reset_password") and a password is never even momentarily part of
    the same request as a role/active change.

    Every field is validated up front, before anything on `user` is
    mutated — a request that fails any check leaves the record completely
    untouched, never partially applied.
    """
    user = db.session.get(User, user_id)
    if user is None:
        return jsonify({"error": "not found"}), 404
    d = request.get_json(force=True) or {}
    actor = current_user()

    new_username = None
    if "username" in d:
        new_username = (d.get("username") or "").strip()
        if not new_username:
            return jsonify({"error": "username cannot be empty"}), 400
        clash = User.query.filter(User.username == new_username, User.id != user.id).first()
        if clash:
            return jsonify({"error": "a user with this username already exists"}), 409

    if "role" in d and d["role"] not in ROLES:
        return jsonify({"error": f"role must be one of {ROLES}"}), 400
    if "active" in d and not isinstance(d["active"], bool):
        return jsonify({"error": "active must be true or false"}), 400

    if user.id == actor.id and "role" in d and d["role"] != ROLE_SUPER_ADMIN:
        return jsonify({"error": "you cannot demote your own account"}), 400
    if user.id == actor.id and "active" in d and not d["active"]:
        return jsonify({"error": "you cannot deactivate your own account"}), 400

    if _is_last_active_super_admin(user):
        if "role" in d and d["role"] != ROLE_SUPER_ADMIN:
            return jsonify({"error": "cannot demote the last active super administrator"}), 400
        if "active" in d and not d["active"]:
            return jsonify({"error": "cannot disable the last active super administrator"}), 400

    before = user.to_dict()
    if new_username is not None:
        user.username = new_username
    if "role" in d:
        user.role = d["role"]
    if "active" in d:
        user.active = bool(d["active"])

    record_audit(actor, "update", "user", entity_id=user.id, before=before, after=user.to_dict())
    db.session.commit()
    return jsonify(user.to_dict())


@admin_users_bp.route("/<int:user_id>/reset-password", methods=["POST"])
@roles_required(ROLE_SUPER_ADMIN)
def reset_password(user_id):
    """
    The only way to change a user's password (there is no self-service
    "change my own password" flow in this app). Bumps session_version so
    every other session already authenticated as this user stops working —
    see webapp/auth.py's current_user(). Never logs the password or its
    hash anywhere, including the audit entry.
    """
    user = db.session.get(User, user_id)
    if user is None:
        return jsonify({"error": "not found"}), 404
    d = request.get_json(force=True) or {}
    password = d.get("password") or ""
    confirm = d.get("confirm_password") or ""

    if len(password) < 8:
        return jsonify({"error": "password must be at least 8 characters"}), 400
    if password != confirm:
        return jsonify({"error": "password and confirmation do not match"}), 400

    before_version = user.session_version or 0
    user.password_hash = generate_password_hash(password)
    user.session_version = before_version + 1

    record_audit(current_user(), "reset_password", "user", entity_id=user.id,
                 before={"session_version": before_version}, after={"session_version": user.session_version})
    db.session.commit()
    return jsonify({"ok": True})


@admin_users_bp.route("/<int:user_id>", methods=["DELETE"])
@roles_required(ROLE_SUPER_ADMIN)
def deactivate_user(user_id):
    """Soft-delete only — operational history and audit rows reference users by id."""
    user = db.session.get(User, user_id)
    if user is None:
        return jsonify({"error": "not found"}), 404
    actor = current_user()
    if user.id == actor.id:
        return jsonify({"error": "you cannot delete your own account"}), 400
    if _is_last_active_super_admin(user):
        return jsonify({"error": "cannot disable the last active super administrator"}), 400

    before = user.to_dict()
    user.active = False
    record_audit(actor, "deactivate", "user", entity_id=user.id, before=before, after=user.to_dict())
    db.session.commit()
    return jsonify({"ok": True})
