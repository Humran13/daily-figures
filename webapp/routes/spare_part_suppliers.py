import logging

from flask import Blueprint, jsonify, request
from sqlalchemy.exc import SQLAlchemyError

from webapp.auth import current_user, roles_required
from webapp.extensions import db
from webapp.models.supplier import Supplier
from webapp.models.user import ROLE_MANAGER, ROLE_SUPER_ADMIN
from webapp.services import spare_part_service as svc
from webapp.services.audit_service import record_audit

spare_part_suppliers_bp = Blueprint("spare_part_suppliers", __name__, url_prefix="/api/spare-parts/suppliers")

logger = logging.getLogger(__name__)


@spare_part_suppliers_bp.route("", methods=["GET"])
@roles_required(ROLE_MANAGER, ROLE_SUPER_ADMIN)
def list_suppliers():
    include_inactive = request.args.get("include_inactive") == "1"
    query = Supplier.query
    if not include_inactive:
        query = query.filter_by(active=True)
    return jsonify([s.to_dict() for s in query.order_by(Supplier.name).all()])


@spare_part_suppliers_bp.route("", methods=["POST"])
@roles_required(ROLE_MANAGER, ROLE_SUPER_ADMIN)
def create_supplier():
    d = request.get_json(force=True) or {}
    name = (d.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name is required"}), 400
    supplier = Supplier(name=name, building=d.get("building"), contacts=d.get("contacts"),
                         notes=d.get("notes"), active=True)
    db.session.add(supplier)
    db.session.flush()
    record_audit(current_user(), "create", "spare_supplier", entity_id=supplier.id, after=supplier.to_dict())
    db.session.commit()
    return jsonify(supplier.to_dict()), 201


@spare_part_suppliers_bp.route("/<int:supplier_id>", methods=["PATCH"])
@roles_required(ROLE_MANAGER, ROLE_SUPER_ADMIN)
def update_supplier(supplier_id):
    supplier = db.session.get(Supplier, supplier_id)
    if supplier is None:
        return jsonify({"error": "not found"}), 404
    d = request.get_json(force=True) or {}
    before = supplier.to_dict()
    if "name" in d:
        name = (d.get("name") or "").strip()
        if not name:
            return jsonify({"error": "name cannot be empty"}), 400
        supplier.name = name
    for field in ("building", "contacts", "notes"):
        if field in d:
            setattr(supplier, field, d.get(field))
    if "active" in d:
        supplier.active = bool(d["active"])
    record_audit(current_user(), "update", "spare_supplier", entity_id=supplier.id, before=before, after=supplier.to_dict())
    db.session.commit()
    return jsonify(supplier.to_dict())


@spare_part_suppliers_bp.route("/<int:supplier_id>", methods=["DELETE"])
@roles_required(ROLE_SUPER_ADMIN)
def delete_supplier(supplier_id):
    supplier = db.session.get(Supplier, supplier_id)
    if supplier is None:
        return jsonify({"error": "not found"}), 404
    try:
        removed = svc.permanently_delete_supplier(supplier, current_user())
        db.session.commit()
    except SQLAlchemyError:
        db.session.rollback()
        logger.exception("permanent delete of supplier %s failed", supplier_id)
        return jsonify({"error": "Delete failed. Nothing was removed; please try again."}), 500
    return jsonify({"ok": True, "removed": removed})
