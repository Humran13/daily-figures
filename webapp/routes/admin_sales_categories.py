from flask import Blueprint, jsonify, request

from webapp.auth import current_user, login_required, roles_required, feature_required
from webapp.extensions import db
from webapp.models.sales_category import SalesCategory
from webapp.models.user import ROLE_SUPER_ADMIN
from webapp.services.audit_service import record_audit

admin_sales_categories_bp = Blueprint(
    "admin_sales_categories", __name__, url_prefix="/api/admin/sales-categories"
)


@admin_sales_categories_bp.route("", methods=["GET"])
@login_required
@feature_required("customer_management")
def list_categories():
    include_inactive = request.args.get("include_inactive") == "1"
    query = SalesCategory.query.order_by(SalesCategory.display_order, SalesCategory.name)
    if not include_inactive:
        query = query.filter_by(active=True)
    return jsonify([c.to_dict() for c in query.all()])


@admin_sales_categories_bp.route("", methods=["POST"])
@roles_required(ROLE_SUPER_ADMIN)
@feature_required("customer_management")
def create_category():
    d = request.get_json(force=True) or {}
    name = (d.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name is required"}), 400
    if SalesCategory.query.filter_by(name=name).first():
        return jsonify({"error": "a sales category with this name already exists"}), 409

    max_order = db.session.query(db.func.max(SalesCategory.display_order)).scalar() or 0
    category = SalesCategory(
        name=name, active=True, display_order=d.get("display_order", max_order + 1),
        created_by=current_user().id,
    )
    db.session.add(category)
    db.session.flush()
    record_audit(current_user(), "create", "sales_category", entity_id=category.id, after=category.to_dict())
    db.session.commit()
    return jsonify(category.to_dict()), 201


@admin_sales_categories_bp.route("/<int:category_id>", methods=["PATCH"])
@roles_required(ROLE_SUPER_ADMIN)
@feature_required("customer_management")
def update_category(category_id):
    category = db.session.get(SalesCategory, category_id)
    if category is None:
        return jsonify({"error": "not found"}), 404
    d = request.get_json(force=True) or {}
    before = category.to_dict()

    if "name" in d:
        new_name = d["name"].strip()
        if not new_name:
            return jsonify({"error": "name cannot be empty"}), 400
        clash = SalesCategory.query.filter(SalesCategory.name == new_name, SalesCategory.id != category.id).first()
        if clash:
            return jsonify({"error": "a sales category with this name already exists"}), 409
        category.name = new_name
    if "display_order" in d:
        category.display_order = int(d["display_order"])
    if "active" in d:
        category.active = bool(d["active"])

    record_audit(current_user(), "update", "sales_category", entity_id=category.id, before=before, after=category.to_dict())
    db.session.commit()
    return jsonify(category.to_dict())
