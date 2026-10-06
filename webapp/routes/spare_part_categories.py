import logging

from flask import Blueprint, jsonify, request
from sqlalchemy.exc import SQLAlchemyError

from webapp.auth import current_user, login_required, roles_required
from webapp.extensions import db
from webapp.models.spare_part_category import SparePartCategory
from webapp.models.user import ROLE_MANAGER, ROLE_SUPER_ADMIN
from webapp.services import spare_part_service as svc
from webapp.services.audit_service import record_audit

spare_part_categories_bp = Blueprint("spare_part_categories", __name__, url_prefix="/api/spare-parts/categories")

logger = logging.getLogger(__name__)


@spare_part_categories_bp.route("", methods=["GET"])
@login_required
def list_categories():
    include_inactive = request.args.get("include_inactive") == "1"
    query = SparePartCategory.query
    if not include_inactive:
        query = query.filter_by(active=True)
    return jsonify([c.to_dict() for c in query.order_by(SparePartCategory.name).all()])


@spare_part_categories_bp.route("", methods=["POST"])
@roles_required(ROLE_MANAGER, ROLE_SUPER_ADMIN)
def create_category():
    d = request.get_json(force=True) or {}
    try:
        category = svc.create_category(d.get("name"))
    except svc.SparePartError as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 400
    record_audit(current_user(), "create", "spare_part_category", entity_id=category.id, after=category.to_dict())
    db.session.commit()
    return jsonify(category.to_dict()), 201


@spare_part_categories_bp.route("/<int:category_id>", methods=["PATCH"])
@roles_required(ROLE_MANAGER, ROLE_SUPER_ADMIN)
def update_category(category_id):
    category = db.session.get(SparePartCategory, category_id)
    if category is None:
        return jsonify({"error": "not found"}), 404
    d = request.get_json(force=True) or {}
    before = category.to_dict()
    try:
        svc.update_category(category, d)
    except svc.SparePartError as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 400
    record_audit(current_user(), "update", "spare_part_category", entity_id=category.id, before=before, after=category.to_dict())
    db.session.commit()
    return jsonify(category.to_dict())


@spare_part_categories_bp.route("/<int:category_id>", methods=["DELETE"])
@roles_required(ROLE_SUPER_ADMIN)
def delete_category(category_id):
    category = db.session.get(SparePartCategory, category_id)
    if category is None:
        return jsonify({"error": "not found"}), 404
    try:
        removed = svc.permanently_delete_category(category, current_user())
        db.session.commit()
    except SQLAlchemyError:
        db.session.rollback()
        logger.exception("permanent delete of category %s failed", category_id)
        return jsonify({"error": "Delete failed. Nothing was removed; please try again."}), 500
    return jsonify({"ok": True, "removed": removed})
