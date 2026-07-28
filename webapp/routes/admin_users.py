from flask import Blueprint, jsonify, request
from werkzeug.security import generate_password_hash

from webapp.auth import current_user, roles_required
from webapp.extensions import db
from webapp.models.user import ROLES, ROLE_SUPER_ADMIN, User
from webapp.services.audit_service import record_audit

admin_users_bp = Blueprint("admin_users", __name__, url_prefix="/api/admin/users")


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
    user = db.session.get(User, user_id)
    if user is None:
        return jsonify({"error": "not found"}), 404
    d = request.get_json(force=True) or {}
    before = user.to_dict()
    actor = current_user()

    if "role" in d:
        if d["role"] not in ROLES:
            return jsonify({"error": f"role must be one of {ROLES}"}), 400
        if user.id == actor.id and d["role"] != ROLE_SUPER_ADMIN:
            return jsonify({"error": "you cannot demote your own account"}), 400
        user.role = d["role"]
    if "active" in d:
        if user.id == actor.id and not d["active"]:
            return jsonify({"error": "you cannot deactivate your own account"}), 400
        user.active = bool(d["active"])
    if "password" in d and d["password"]:
        if len(d["password"]) < 8:
            return jsonify({"error": "password must be at least 8 characters"}), 400
        user.password_hash = generate_password_hash(d["password"])

    record_audit(actor, "update", "user", entity_id=user.id, before=before, after=user.to_dict())
    db.session.commit()
    return jsonify(user.to_dict())


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

    before = user.to_dict()
    user.active = False
    record_audit(actor, "deactivate", "user", entity_id=user.id, before=before, after=user.to_dict())
    db.session.commit()
    return jsonify({"ok": True})
